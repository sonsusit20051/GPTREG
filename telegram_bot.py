"""
Telegram bot điều khiển luồng reg -> lấy link pay.

Chạy:
    TELEGRAM_BOT_TOKEN="..." .venv/bin/python3 telegram_bot.py
"""

from __future__ import annotations

import json
import os
import re
import builtins
import csv
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import requests

import main
import browser
try:
    import cleanup_gpm_profiles
except ModuleNotFoundError:
    cleanup_gpm_profiles = None
from email_service import HotmailAccount, create_temp_email
from tempmail_service import (
    add_blocked_domains,
    clear_blocked_domains,
    describe_domain_mode,
    get_blocked_domains,
    remove_blocked_domains,
    set_domain_mode_all,
    set_focus_domains,
)


API_BASE = "https://api.telegram.org/bot{token}/{method}"
STATE_FILE = Path(__file__).with_name("telegram_bot_state.json")
MAX_USER_BUNDLES = 1
MAX_ADMIN_PARALLEL = 4
MAX_GLOBAL_BROWSERS = 4
DEFAULT_USER_CREDITS = 1
JOB_STALL_TIMEOUT = int(os.environ.get("TELEGRAM_JOB_STALL_TIMEOUT", "240"))
REGGET_SINGLE_WINDOW_WIDTH = int(os.environ.get("REGGET_SINGLE_WINDOW_WIDTH", "1500"))
REGGET_SINGLE_WINDOW_HEIGHT = int(os.environ.get("REGGET_SINGLE_WINDOW_HEIGHT", "980"))

_state_lock = threading.Lock()
_user_locks: dict[int, threading.Lock] = {}
_user_slots: dict[int, threading.BoundedSemaphore] = {}
_user_locks_guard = threading.Lock()
_global_browser_slots = threading.BoundedSemaphore(MAX_GLOBAL_BROWSERS)
_stop_events: dict[int, threading.Event] = {}
_current_drivers: dict[int, list[Any]] = {}
_driver_lock = threading.Lock()


def _log(message: str) -> None:
    timestamp = time.strftime("%H:%M:%S")
    builtins.print(f"[{timestamp}] {message}", flush=True)


def _stop_event_for(user_id: int) -> threading.Event:
    with _driver_lock:
        event = _stop_events.get(user_id)
        if not event:
            event = threading.Event()
            _stop_events[user_id] = event
        return event


def _register_driver(user_id: int, driver: Any) -> None:
    with _driver_lock:
        drivers = _current_drivers.setdefault(user_id, [])
        if driver not in drivers:
            drivers.append(driver)


def _clear_driver(user_id: int, driver: Any = None) -> None:
    with _driver_lock:
        if driver is None:
            _current_drivers.pop(user_id, None)
            return
        drivers = _current_drivers.get(user_id, [])
        _current_drivers[user_id] = [item for item in drivers if item is not driver]
        if not _current_drivers[user_id]:
            _current_drivers.pop(user_id, None)


def _close_user_drivers(user_id: int, reason: str = "") -> bool:
    with _driver_lock:
        drivers = list(_current_drivers.get(user_id, []))

    closed_any = False
    for driver in drivers:
        profile_id = getattr(driver, "gpm_profile_id", "")
        try:
            driver.quit()
            closed_any = True
        except Exception as e:
            suffix = f" ({reason})" if reason else ""
            _log(f"Đóng trình duyệt thất bại{suffix}: {e}")
        finally:
            if profile_id:
                try:
                    browser.cleanup_active_gpm_profiles(reason=reason or "bot cleanup")
                except Exception as e:
                    _log(f"Cleanup GPM profile thất bại: {e}")

    if drivers:
        _clear_driver(user_id)
    return closed_any


def _request_stop_job(user_id: int) -> bool:
    event = _stop_event_for(user_id)
    event.set()

    return event.is_set() or _close_user_drivers(user_id, reason="/stop")


def _request_done_job(user_id: int) -> bool:
    event = _stop_event_for(user_id)
    event.set()
    closed = _close_user_drivers(user_id, reason="/done")
    try:
        browser.cleanup_active_gpm_profiles(reason="/done cleanup")
    except Exception as e:
        _log(f"Cleanup GPM profile sau /done thất bại: {e}")
    _clear_driver(user_id)
    return event.is_set() or closed


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "admin_ids": [],
            "banned_user_ids": [],
            "banned_usernames": [],
            "default_password": "",
            "user_credits": {},
            "user_threads": {},
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    data.setdefault("admin_ids", [])
    data.setdefault("banned_user_ids", [])
    data.setdefault("banned_usernames", [])
    data.setdefault("default_password", "")
    data.setdefault("user_credits", {})
    data.setdefault("user_threads", {})
    return data


def _save_state(state: dict[str, Any]) -> None:
    tmp_path = STATE_FILE.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp_path.replace(STATE_FILE)


def _configured_admin_ids() -> list[int]:
    raw = os.environ.get("TELEGRAM_ADMIN_IDS", "")
    ids = []
    for item in raw.split(","):
        item = item.strip()
        if item.isdigit():
            ids.append(int(item))
    return ids


def _bootstrap_admin_if_needed(user_id: int) -> None:
    with _state_lock:
        state = _load_state()
        state.setdefault("user_credits", {}).setdefault(str(user_id), DEFAULT_USER_CREDITS)
        state.setdefault("user_threads", {}).setdefault(str(user_id), 1)
        configured = _configured_admin_ids()
        if configured:
            merged = sorted(set(state["admin_ids"]) | set(configured))
            if merged != state["admin_ids"]:
                state["admin_ids"] = merged
                _save_state(state)
            return

        if not state["admin_ids"]:
            state["admin_ids"] = [user_id]
            _save_state(state)


def _is_admin(user_id: int) -> bool:
    with _state_lock:
        state = _load_state()
        return user_id in set(state["admin_ids"]) | set(_configured_admin_ids())


def _admin_ids() -> list[int]:
    with _state_lock:
        state = _load_state()
        return sorted(set(state["admin_ids"]) | set(_configured_admin_ids()))


def _is_banned(user_id: int, username: str = "") -> bool:
    username = (username or "").lower().lstrip("@")
    with _state_lock:
        state = _load_state()
        return (
            user_id in set(state["banned_user_ids"])
            or bool(username and username in set(state["banned_usernames"]))
        )


def _get_credits(user_id: int) -> int:
    with _state_lock:
        state = _load_state()
        credits = state.setdefault("user_credits", {})
        key = str(user_id)
        if key not in credits:
            credits[key] = DEFAULT_USER_CREDITS
            _save_state(state)
        return int(credits.get(key, 0))


def _sync_user_credit_identity(user_id: int, username: str = "") -> int:
    username_key = (username or "").lower().lstrip("@")
    with _state_lock:
        state = _load_state()
        credits = state.setdefault("user_credits", {})
        threads = state.setdefault("user_threads", {})
        id_key = str(user_id)
        if id_key not in credits:
            credits[id_key] = DEFAULT_USER_CREDITS
        if username_key and username_key in credits:
            credits[id_key] = int(credits.get(id_key, 0)) + int(credits.pop(username_key, 0))
        if id_key not in threads:
            threads[id_key] = 1
        if username_key and username_key in threads:
            threads[id_key] = max(int(threads.get(id_key, 1)), int(threads.pop(username_key, 1)))
        _save_state(state)
        return int(credits.get(id_key, 0))


def _consume_credit(user_id: int, amount: int = 1) -> int:
    with _state_lock:
        state = _load_state()
        credits = state.setdefault("user_credits", {})
        key = str(user_id)
        current = int(credits.get(key, DEFAULT_USER_CREDITS))
        current = max(0, current - amount)
        credits[key] = current
        _save_state(state)
        return current


def _add_credit(target: str, amount: int) -> str:
    target = target.strip()
    if not target:
        raise ValueError("Thiếu username/id")
    if amount <= 0:
        raise ValueError("Số credit phải lớn hơn 0")

    with _state_lock:
        state = _load_state()
        credits = state.setdefault("user_credits", {})
        key = target if target.lstrip("-").isdigit() else target.lower().lstrip("@")
        current = int(credits.get(key, DEFAULT_USER_CREDITS if key.lstrip("-").isdigit() else 0))
        credits[key] = current + amount
        _save_state(state)
        label = f"id {key}" if key.lstrip("-").isdigit() else f"@{key}"
        return f"{label}: {credits[key]} credit"


def _thread_limit_for(user_id: int, is_admin: bool = False) -> int:
    if is_admin:
        return MAX_ADMIN_PARALLEL
    with _state_lock:
        state = _load_state()
        threads = state.setdefault("user_threads", {})
        return max(1, min(MAX_GLOBAL_BROWSERS, int(threads.get(str(user_id), 1))))


def _add_thread_limit(target: str) -> str:
    target = target.strip()
    if not target:
        raise ValueError("Thiếu username/id")

    with _state_lock:
        state = _load_state()
        threads = state.setdefault("user_threads", {})
        key = target if target.lstrip("-").isdigit() else target.lower().lstrip("@")
        threads[key] = MAX_GLOBAL_BROWSERS
        _save_state(state)
        label = f"id {key}" if key.lstrip("-").isdigit() else f"@{key}"
        return f"{label}: tối đa {MAX_GLOBAL_BROWSERS} luồng"


def _default_password() -> str:
    with _state_lock:
        return str(_load_state().get("default_password") or "").strip()


def _set_default_password(password: str) -> None:
    with _state_lock:
        state = _load_state()
        state["default_password"] = password
        _save_state(state)


def _ban_target(target: str) -> str:
    target = target.strip()
    if not target:
        raise ValueError("Thiếu username/id")

    with _state_lock:
        state = _load_state()
        if target.lstrip("-").isdigit():
            user_id = int(target)
            if user_id not in state["banned_user_ids"]:
                state["banned_user_ids"].append(user_id)
            _save_state(state)
            return f"id {user_id}"

        username = target.lower().lstrip("@")
        if username not in state["banned_usernames"]:
            state["banned_usernames"].append(username)
        _save_state(state)
        return f"@{username}"


def _unban_target(target: str) -> str:
    target = target.strip()
    if not target:
        raise ValueError("Thiếu username/id")

    with _state_lock:
        state = _load_state()
        if target.lstrip("-").isdigit():
            user_id = int(target)
            state["banned_user_ids"] = [item for item in state["banned_user_ids"] if item != user_id]
            _save_state(state)
            return f"id {user_id}"

        username = target.lower().lstrip("@")
        state["banned_usernames"] = [item for item in state["banned_usernames"] if item != username]
        _save_state(state)
        return f"@{username}"


def _user_lock(user_id: int) -> threading.Lock:
    with _user_locks_guard:
        lock = _user_locks.get(user_id)
        if not lock:
            lock = threading.Lock()
            _user_locks[user_id] = lock
        return lock


def _user_slot(user_id: int, is_admin: bool = False) -> threading.BoundedSemaphore:
    limit = _thread_limit_for(user_id, is_admin=is_admin)
    with _user_locks_guard:
        slot = _user_slots.get(user_id)
        current_limit = getattr(slot, "_initial_value", None) if slot else None
        if not slot or current_limit != limit:
            slot = threading.BoundedSemaphore(limit)
            _user_slots[user_id] = slot
        return slot


def _api_url(token: str, method: str) -> str:
    return API_BASE.format(token=token, method=method)


def send_message(token: str, chat_id: int, text: str) -> None:
    last_message_id = None
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for chunk in chunks:
        response = requests.post(
            _api_url(token, "sendMessage"),
            json={"chat_id": chat_id, "text": chunk},
            timeout=20,
        )
        try:
            data = response.json()
            last_message_id = data.get("result", {}).get("message_id") or last_message_id
        except Exception:
            pass
    return last_message_id


def send_document(token: str, chat_id: int, file_path: str, caption: str = "") -> None:
    with open(file_path, "rb") as f:
        requests.post(
            _api_url(token, "sendDocument"),
            data={"chat_id": chat_id, "caption": caption},
            files={"document": f},
            timeout=60,
        ).raise_for_status()


def _download_telegram_text_document(token: str, document: dict[str, Any]) -> str:
    file_name = str(document.get("file_name") or "").strip()
    mime_type = str(document.get("mime_type") or "").strip().lower()
    if file_name and not file_name.lower().endswith(".txt"):
        raise ValueError("File phải là .txt")
    if mime_type and mime_type not in {"text/plain", "application/octet-stream"}:
        raise ValueError(f"File txt không hợp lệ, mime_type={mime_type}")

    file_id = str(document.get("file_id") or "").strip()
    if not file_id:
        raise ValueError("Không tìm thấy file_id của document")

    meta_resp = requests.get(_api_url(token, "getFile"), params={"file_id": file_id}, timeout=30)
    meta_resp.raise_for_status()
    meta = meta_resp.json().get("result") or {}
    file_path = str(meta.get("file_path") or "").strip()
    if not file_path:
        raise ValueError("Telegram không trả file_path cho document")

    file_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    content = requests.get(file_url, timeout=60)
    content.raise_for_status()
    raw = content.content
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _build_regget_csv(results: list[tuple[Any, dict[str, Any]]]) -> str:
    with NamedTemporaryFile("w", encoding="utf-8-sig", newline="", suffix=".csv", delete=False) as tmp:
        writer = csv.writer(tmp)
        writer.writerow(["bundle", "checkout_url"])
        for account, result in results:
            writer.writerow([
                _bundle(account),
                str(result.get("checkout_url") or "").strip(),
            ])
        return tmp.name


def edit_message(token: str, chat_id: int, message_id: int, text: str) -> bool:
    try:
        response = requests.post(
            _api_url(token, "editMessageText"),
            json={"chat_id": chat_id, "message_id": message_id, "text": text},
            timeout=20,
        )
        if response.status_code == 400 and "message is not modified" in response.text.lower():
            return True
        return response.ok
    except Exception as e:
        _log(f"Sửa message tiến trình thất bại: {e}")
        return False


class TelegramProgress:
    STAGES = [
        (5, "Khởi động job", ("bắt đầu xử lý",)),
        (10, "Mở profile trình duyệt", ("đang mở gpm", "đang khởi tạo trình duyệt", "attach selenium")),
        (15, "Trình duyệt sẵn sàng", ("trình duyệt đã khởi tạo", "chromedriver session đã sẵn sàng")),
        (25, "Mở trang đăng ký", ("đang mở https://chatgpt.com/auth/login",)),
        (35, "Nhập email", ("đã nhập email", "click nút tiếp tục")),
        (45, "Chờ mã OTP", ("đang chờ mã xác minh", "chế độ otp nhanh")),
        (55, "Đã lấy OTP", ("đã lấy được mã xác minh", "tìm thấy otp")),
        (65, "Đã nhập OTP", ("đã nhập mã xác minh", "otp được chấp nhận")),
        (75, "Điền hồ sơ", ("đã nhập họ tên", "đã nhập tuổi", "đã gửi thông tin đăng ký")),
        (85, "Vào ChatGPT", ("đã thoát khỏi about-you", "đăng ký thành công")),
        (95, "Lấy link pay", ("đang lấy link", "đang tạo checkout trial mới", "đã lấy được checkout link")),
        (100, "Hoàn tất", ("output link", "hoàn tất")),
    ]

    def __init__(self, token: str, chat_id: int, user_id: int, username: str = ""):
        self.token = token
        self.chat_id = chat_id
        self.user_id = user_id
        self.username = username
        self.percent = 0
        self.stage = "Đang chờ"
        self.last_sent = 0.0
        self.last_activity = time.time()
        self.message_id = None
        self.last_text = ""
        self.lock = threading.Lock()

    def touch(self) -> None:
        with self.lock:
            self.last_activity = time.time()

    def idle_seconds(self) -> float:
        with self.lock:
            return time.time() - self.last_activity

    def log(self, message: str, force: bool = False) -> None:
        message = str(message).strip()
        if not message:
            return

        _log(f"{_user_label(self.user_id, self.username)} | {message}")
        lowered = message.lower()
        with self.lock:
            self.last_activity = time.time()
            changed = False
            for percent, stage, keywords in self.STAGES:
                if percent > self.percent and any(keyword in lowered for keyword in keywords):
                    self.percent = percent
                    self.stage = stage
                    changed = True

            now = time.time()
            if force or (changed and now - self.last_sent >= 3) or now - self.last_sent >= 30:
                self._send_locked()

    def set(self, percent: int, stage: str, force: bool = True) -> None:
        _log(f"{_user_label(self.user_id, self.username)} | {percent}% {stage}")
        with self.lock:
            self.last_activity = time.time()
            if percent >= self.percent:
                self.percent = min(100, max(0, int(percent)))
                self.stage = stage
            if force:
                self._send_locked()

    def heartbeat(self) -> None:
        _log(f"{_user_label(self.user_id, self.username)} | heartbeat {self.percent}%")
        with self.lock:
            if self.percent < 94:
                self.percent += 1
            self._send_locked()

    def _send_locked(self) -> None:
        text = f"Đang thực hiện tiến trình\n{self.percent}%"
        if text == self.last_text:
            return

        self.last_sent = time.time()
        self.last_text = text
        if self.message_id:
            if edit_message(self.token, self.chat_id, self.message_id, text):
                return

        self.message_id = send_message(self.token, self.chat_id, text)

    def flush(self) -> None:
        with self.lock:
            self._send_locked()


def _clean_payload(text: str) -> str:
    text = re.sub(r"^/regget(?:@\w+)?", "", text, count=1).strip()
    text = text.replace("```", "").strip()
    return text


def _parse_bundles(payload: str) -> list[HotmailAccount]:
    accounts = []
    for raw_line in payload.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 4 or not all(parts):
            raise ValueError(f"Dòng không đúng format: {line[:80]}")
        accounts.append(
            HotmailAccount(
                email=parts[0],
                password=parts[1],
                refresh_token=parts[2],
                client_id=parts[3],
            )
        )
    return accounts


def _bundle(account: Any) -> str:
    email = str(getattr(account, "email", "") or "").strip()
    password = str(getattr(account, "password", "") or "").strip()
    refresh_token = str(getattr(account, "refresh_token", "") or "").strip()
    client_id = str(getattr(account, "client_id", "") or "").strip()
    if refresh_token or client_id or password:
        return "|".join((email, password, refresh_token, client_id))
    return email


def _result_bundle(result: dict[str, Any], account: Any) -> str:
    email = str(result.get("email") or getattr(account, "email", "") or "").strip()
    password = str(result.get("password") or getattr(account, "password", "") or "").strip()
    twofa_secret = str(result.get("twofa_secret") or "").strip()
    return "|".join((email, password, twofa_secret))


def _format_timings(result: dict[str, Any]) -> str:
    timings = result.get("timings") or {}
    if not isinstance(timings, dict) or not timings:
        return ""

    keys = (
        "init_browser",
        "open_page",
        "fill_signup_form",
        "otp_total",
        "fill_profile_info",
        "trial_free",
        "get_pay_link",
        "setup_2fa",
        "total",
    )
    parts = []
    for key in keys:
        value = timings.get(key)
        if value is not None:
            parts.append(f"{key}={value}s")
    return " | ".join(parts)


def _format_account_result(account: HotmailAccount, result: dict[str, Any], remaining_credit: int | None = None) -> str:
    pay_link = str(result.get("checkout_url") or "").strip()
    reason = str(result.get("failure_reason") or "Thất bại")

    if result.get("success") and result.get("manual_checkout_ready"):
        return f"MANUAL_CHECKOUT\n{_result_bundle(result, account)}\nĐã mở trang checkout, giữ trình duyệt để thanh toán tay"
    if result.get("success") and pay_link:
        return f"{_result_bundle(result, account)}\n{pay_link}"
    if result.get("success") and result.get("trial_success"):
        return f"TRIAL_FREE\n{_result_bundle(result, account)}\nĐã tạo tài khoản và chạy trial_free thành công"
    if result.get("success") and result.get("no_trial"):
        return f"NO_TRIAL\n{_result_bundle(result, account)}\nĐã tạo tài khoản nhưng account không có trial"
    if result.get("success") and reason and reason not in {"Đăng ký thất bại", "Thất bại"}:
        return f"FAIL\n{_result_bundle(result, account)}\n{reason}"
    if result.get("success"):
        return f"REGISTERED\n{_result_bundle(result, account)}\nĐã tạo tài khoản thành công, chưa chạy trial thành công"

    return f"FAIL\n{_result_bundle(result, account)}\n{reason}"


def _finish_one_account(
    token: str,
    chat_id: int,
    user_id: int,
    username: str,
    account: Any,
    result: dict[str, Any],
    is_admin: bool,
    progress: TelegramProgress,
    results: list[tuple[HotmailAccount, dict[str, Any]]],
) -> None:
    results.append((account, result))
    if result.get("success") and (result.get("checkout_url") or result.get("trial_success") or result.get("manual_checkout_ready")):
        if result.get("manual_checkout_ready"):
            done_label = "đã mở checkout tay"
        else:
            done_label = "đã chạy trial" if result.get("trial_success") else "đã có link pay"
        progress.set(100, f"Hoàn tất {account.email}: {done_label}")
        remaining = None if is_admin else _consume_credit(user_id, 1)
    else:
        progress.set(progress.percent, f"Hoàn tất {account.email}: chưa thành công")
        remaining = None

    send_message(token, chat_id, _format_account_result(account, result, remaining_credit=remaining))
    _notify_admins_regget_done(token, chat_id, user_id, username, [(account, result)])


def _run_one_account(
    account: HotmailAccount,
    user_id: int,
    stop_event: threading.Event,
    progress: TelegramProgress | None = None,
) -> dict[str, Any]:
    with _global_browser_slots:
        if stop_event.is_set():
            raise InterruptedError("Người dùng đã dừng job")
        if progress:
            progress.set(5, f"Bắt đầu xử lý {account.email}")
        result: dict[str, Any] | None = None

        def monitor(driver, step):
            _register_driver(user_id, driver)
            if progress:
                progress.touch()
                stage_map = {
                    "init_browser": (15, "Trình duyệt sẵn sàng"),
                    "open_page": (25, "Mở trang đăng ký"),
                    "fill_form": (45, "Chờ mã OTP"),
                    "enter_code": (70, "OTP đã được chấp nhận"),
                    "fill_profile": (85, "Đã hoàn tất hồ sơ"),
                    "registered": (92, "Đang chạy trial"),
                    "checkout_link": (95, "Đang lấy link pay"),
                    "setup_2fa": (98, "Đang bật 2FA"),
                }
                if step in stage_map:
                    percent, stage = stage_map[step]
                    progress.set(percent, stage, force=True)
            if stop_event.is_set():
                raise InterruptedError("Người dùng đã dừng job")

        try:
            result = main.register_one_account_with_profile_retries(
                monitor_callback=monitor,
                email_context_override=account,
                account_password_override=_default_password() or None,
                return_details=True,
                mark_result=False,
            )
            return result
        finally:
            if not (result and result.get("manual_checkout_ready")):
                _clear_driver(user_id)


def _user_label(user_id: int, username: str = "") -> str:
    username = (username or "").strip()
    if username:
        return f"@{username} | id {user_id}"
    return f"id {user_id}"


def _notify_admins_regget_done(
    token: str,
    requester_chat_id: int,
    user_id: int,
    username: str,
    results: list[tuple[Any, dict[str, Any]]],
) -> None:
    lines = [
        "Thông báo /regget hoàn tất",
        f"User: {_user_label(user_id, username)}",
        "",
    ]

    for account, result in results:
        pay_link = str(result.get("checkout_url") or "").strip()
        reason = str(result.get("failure_reason") or "Thất bại")
        if result.get("success") and result.get("manual_checkout_ready"):
            status = "MANUAL_CHECKOUT"
            output = "Đã mở trang checkout, giữ trình duyệt để thanh toán tay"
        elif result.get("success") and pay_link:
            status = "SUCCESS"
            output = f"{_result_bundle(result, account)}\n{pay_link}"
        elif result.get("success") and result.get("trial_success"):
            status = "TRIAL_FREE"
            output = "Đã tạo tài khoản và chạy trial_free thành công"
        elif result.get("success") and reason and reason not in {"Đăng ký thất bại", "Thất bại"}:
            status = "FAIL"
            output = reason
        elif result.get("success"):
            status = "REGISTERED"
            output = "Đã tạo tài khoản thành công, chưa chạy trial thành công"
        else:
            status = "FAIL"
            output = reason

        if result.get("success") and pay_link:
            lines.extend([
                status,
                output,
                "",
            ])
        else:
            lines.extend([
                status,
                f"Cụm mail: {_bundle(account)}",
                f"Kết quả: {output}",
                "",
            ])

    message = "\n".join(lines).strip()
    for admin_id in _admin_ids():
        if admin_id == requester_chat_id:
            continue
        send_message(token, admin_id, message)


def _run_regget_job(
    token: str,
    chat_id: int,
    user_id: int,
    username: str,
    accounts: list[Any],
    is_admin: bool,
) -> None:
    slot = _user_slot(user_id, is_admin=is_admin)
    if not slot.acquire(blocking=False):
        limit = _thread_limit_for(user_id, is_admin=is_admin)
        send_message(token, chat_id, f"Bạn đang chạy đủ {limit} luồng. Chờ một job xong rồi gửi tiếp.")
        return

    try:
        stop_event = _stop_event_for(user_id)
        stop_event.clear()
        progress = TelegramProgress(token, chat_id, user_id, username)
        done_event = threading.Event()

        def watchdog():
            while not done_event.wait(5):
                if stop_event.is_set():
                    _close_user_drivers(user_id, reason="đã nhận lệnh dừng")
                    return

                idle = progress.idle_seconds()
                if idle >= JOB_STALL_TIMEOUT:
                    stop_event.set()
                    progress.log(
                        f"Không thấy tiến triển trong {JOB_STALL_TIMEOUT} giây, tự dừng job và đóng profile.",
                        force=True,
                    )
                    progress.set(progress.percent, "Job bị đơ, đang đóng profile")
                    _close_user_drivers(user_id, reason="job bị đơ")
                    return

        def heartbeat():
            while not done_event.wait(4):
                if stop_event.is_set():
                    return
                progress.heartbeat()

        threading.Thread(target=watchdog, daemon=True).start()
        threading.Thread(target=heartbeat, daemon=True).start()
        progress.set(1, "Đã nhận lệnh")
        _log(f"{_user_label(user_id, username)} bắt đầu /regget với {len(accounts)} cụm mail")
        results: list[tuple[Any, dict[str, Any]]] = []
        single_account_large_window = len(accounts) == 1
        if single_account_large_window:
            browser.set_visible_grid_override(
                cols=1,
                rows=1,
                width=REGGET_SINGLE_WINDOW_WIDTH,
                height=REGGET_SINGLE_WINDOW_HEIGHT,
            )
            browser.set_profile_zoom_override(1.0)
            progress.log(
                f"Chỉ có 1 mail, mở trình duyệt kích thước lớn {REGGET_SINGLE_WINDOW_WIDTH}x{REGGET_SINGLE_WINDOW_HEIGHT} để debug 2FA",
                force=True,
            )

        if is_admin and len(accounts) > 1:
            with ThreadPoolExecutor(max_workers=min(MAX_ADMIN_PARALLEL, len(accounts))) as executor:
                future_map = {
                    executor.submit(_run_one_account, account, user_id, stop_event, progress): account
                    for account in accounts
                }
                for future in as_completed(future_map):
                    account = future_map[future]
                    try:
                        result = future.result()
                    except InterruptedError:
                        progress.set(progress.percent, f"Đã dừng {account.email}")
                        result = {"success": False, "failure_reason": "Đã dừng theo lệnh /stop", "checkout_url": ""}
                    except Exception as e:
                        progress.log(f"Lỗi khi xử lý {account.email}: {e}", force=True)
                        result = {"success": False, "failure_reason": str(e), "checkout_url": ""}
                    _finish_one_account(token, chat_id, user_id, username, account, result, is_admin, progress, results)
        else:
            for account in accounts:
                if stop_event.is_set():
                    progress.log("Job đã được dừng trước khi chạy tài khoản tiếp theo", force=True)
                    break
                progress.log(f"Đang chạy tài khoản: {account.email}", force=True)
                try:
                    result = _run_one_account(account, user_id, stop_event, progress)
                except InterruptedError:
                    progress.set(progress.percent, f"Đã dừng {account.email}")
                    result = {"success": False, "failure_reason": "Đã dừng theo lệnh /stop", "checkout_url": ""}
                except Exception as e:
                    progress.log(f"Lỗi khi xử lý {account.email}: {e}", force=True)
                    result = {"success": False, "failure_reason": str(e), "checkout_url": ""}
                _finish_one_account(token, chat_id, user_id, username, account, result, is_admin, progress, results)
        if len(accounts) > 1 and results:
            csv_path = _build_regget_csv(results)
            try:
                send_document(token, chat_id, csv_path, caption="Kết quả /regget CSV")
            finally:
                try:
                    os.unlink(csv_path)
                except OSError:
                    pass
        progress.flush()
    finally:
        try:
            browser.set_visible_grid_override()
            browser.set_profile_zoom_override()
        except Exception:
            pass
        if "done_event" in locals():
            done_event.set()
        slot.release()


def _help_text(is_admin: bool) -> str:
    return (
        "Menu hướng dẫn sử dụng bot\n\n"
        "1. Đăng ký và lấy link thanh toán\n"
        "Gửi lệnh theo mẫu:\n"
        "/regget email|mật_khẩu_mail|refresh_token|client_id\n\n"
        "Hoặc gửi file .txt kèm caption /regget.\n"
        "Mỗi dòng trong file: email|mật_khẩu_mail|refresh_token|client_id\n\n"
        "Hoặc chạy nhanh theo file Hotmail cũ:\n"
        "/regtmail 4\n"
        "/regtmail 2\n\n"
        "Alias nhanh trên menu:\n"
        "/regtmail4\n"
        "/regtmail2\n\n"
        "Kết quả trả về:\n"
        "✅ Checkout\n"
        "📦 email|mật_khẩu_mail|refresh_token|client_id\n"
        "🔗 link_pay\n\n"
        "Nếu gửi nhiều mail trong /regget, bot sẽ gửi thêm file CSV gồm 2 cột: bundle, checkout_url.\n\n"
        "2. Giới hạn user thường\n"
        "- Mỗi user chỉ được chạy 1 job tại một thời điểm.\n"
        "- Mỗi lệnh /regget chỉ gửi 1 cụm mail.\n"
        "- Mỗi người mặc định có 1 credit.\n"
        "- Khi lấy link pay thành công, hệ thống trừ 1 credit.\n\n"
        "3. Dừng tiến trình đang chạy\n"
        "/stop\n\n"
        "4. Đóng và xoá toàn bộ profile đang chạy\n"
        "/done\n\n"
        "Nếu job bị đơ quá lâu, bot sẽ tự đóng profile để tránh treo.\n"
    )


def _telegram_menu_commands() -> list[dict[str, str]]:
    return [
        {"command": "regtmail4", "description": "Chạy nhanh 4 acc Hotmail"},
        {"command": "regtmail2", "description": "Chạy nhanh 2 acc Hotmail"},
        {"command": "regtmail", "description": "Chạy Hotmail từ file, ví dụ /regtmail 4"},
        {"command": "stop", "description": "Dừng job hiện tại"},
        {"command": "done", "description": "Đóng và xoá toàn bộ profile đang chạy"},
        {"command": "menu", "description": "Xem hướng dẫn"},
    ]


def _set_bot_commands(token: str) -> None:
    try:
        requests.post(
            _api_url(token, "setMyCommands"),
            json={"commands": _telegram_menu_commands()},
            timeout=20,
        ).raise_for_status()
        _log("Đã cập nhật menu lệnh Telegram")
    except Exception as e:
        _log(f"Cập nhật menu lệnh Telegram thất bại: {e}")


def _run_regtmail_job(
    token: str,
    chat_id: int,
    user_id: int,
    username: str,
    is_admin: bool,
    quantity: int = 1,
) -> None:
    slot = _user_slot(user_id, is_admin=is_admin)
    if not slot.acquire(blocking=False):
        limit = _thread_limit_for(user_id, is_admin=is_admin)
        send_message(token, chat_id, f"Bạn đang chạy đủ {limit} luồng. Chờ một job xong rồi gửi tiếp.")
        return

    try:
        stop_event = _stop_event_for(user_id)
        stop_event.clear()
        progress = TelegramProgress(token, chat_id, user_id, username)
        done_event = threading.Event()

        def watchdog():
            while not done_event.wait(5):
                if stop_event.is_set():
                    _close_user_drivers(user_id, reason="đã nhận lệnh dừng")
                    return
                idle = progress.idle_seconds()
                if idle >= JOB_STALL_TIMEOUT:
                    stop_event.set()
                    progress.log(
                        f"Không thấy tiến triển trong {JOB_STALL_TIMEOUT} giây, tự dừng job và đóng profile.",
                        force=True,
                    )
                    progress.set(progress.percent, "Job bị đơ, đang đóng profile")
                    _close_user_drivers(user_id, reason="job bị đơ")
                    return

        def heartbeat():
            while not done_event.wait(4):
                if stop_event.is_set():
                    return
                progress.heartbeat()

        threading.Thread(target=watchdog, daemon=True).start()
        threading.Thread(target=heartbeat, daemon=True).start()
        progress.set(1, "Đã nhận lệnh chạy Hotmail")
        _log(f"{_user_label(user_id, username)} bắt đầu luồng Hotmail với số lượng {quantity}")

        if not is_admin and _get_credits(user_id) < quantity:
            send_message(token, chat_id, f"Bạn không đủ credit. Hiện có {_get_credits(user_id)} credit, cần {quantity}.")
            return

        results: list[tuple[Any, dict[str, Any]]] = []
        created_accounts: list[Any] = []
        progress.log(f"Đang lấy {quantity} tài khoản Hotmail từ file...", force=True)
        for idx in range(quantity):
            if stop_event.is_set():
                progress.log("Job đã được dừng trong lúc lấy Hotmail", force=True)
                break
            _, account = create_temp_email()
            if not account:
                progress.log(f"Không lấy được Hotmail thứ {idx + 1}/{quantity}", force=True)
                break
            created_accounts.append(account)

        if not created_accounts:
            send_message(token, chat_id, "Không lấy được tài khoản Hotmail từ file.")
            return

        max_parallel = min(4, MAX_GLOBAL_BROWSERS)
        progress.log(
            f"Bắt đầu chạy {len(created_accounts)} acc Hotmail, tối đa {max_parallel} luồng đồng thời...",
            force=True,
        )

        for batch_start in range(0, len(created_accounts), max_parallel):
            if stop_event.is_set():
                break
            batch = created_accounts[batch_start:batch_start + max_parallel]
            batch_size = len(batch)
            if batch_size <= 1:
                browser.set_visible_grid_override(cols=1, rows=1)
                browser.set_profile_zoom_override(1.0)
            elif batch_size == 2:
                browser.set_visible_grid_override(
                    cols=2,
                    rows=1,
                    width=browser.VISIBLE_WINDOW_WIDTH,
                    height=browser.VISIBLE_WINDOW_HEIGHT * 2,
                )
                browser.set_profile_zoom_override(1.0)
            else:
                browser.set_visible_grid_override(cols=2, rows=2)
                browser.set_profile_zoom_override(1.0)
            progress.log(
                f"Đang xử lý batch {batch_start // max_parallel + 1}: {len(batch)} acc",
                force=True,
            )
            try:
                with ThreadPoolExecutor(max_workers=batch_size) as executor:
                    future_map = {
                        executor.submit(_run_one_account, account, user_id, stop_event, progress): account
                        for account in batch
                    }
                    for future in as_completed(future_map):
                        account = future_map[future]
                        try:
                            result = future.result()
                        except InterruptedError:
                            progress.set(progress.percent, f"Đã dừng {account.email}")
                            result = {"success": False, "failure_reason": "Đã dừng theo lệnh /stop", "checkout_url": ""}
                        except Exception as e:
                            progress.log(f"Lỗi khi xử lý {account.email}: {e}", force=True)
                            result = {"success": False, "failure_reason": str(e), "checkout_url": ""}
                        _finish_one_account(token, chat_id, user_id, username, account, result, is_admin, progress, results)
            finally:
                browser.set_visible_grid_override()
                browser.set_profile_zoom_override()
        progress.flush()
    finally:
        if "done_event" in locals():
            done_event.set()
        slot.release()


def handle_update(token: str, update: dict[str, Any]) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    document = message.get("document") or {}
    chat_id = int(chat.get("id"))
    user_id = int(sender.get("id"))
    username = str(sender.get("username") or "")
    text = str(message.get("text") or message.get("caption") or "").strip()
    if not text:
        return

    _bootstrap_admin_if_needed(user_id)
    _sync_user_credit_identity(user_id, username)
    is_admin = _is_admin(user_id)

    if _is_banned(user_id, username) and not is_admin:
        send_message(token, chat_id, "Bạn đã bị ban.")
        return

    command_parts = text.split(maxsplit=1)
    command = command_parts[0] if command_parts else ""
    args = command_parts[1] if len(command_parts) > 1 else ""
    command = command.split("@", 1)[0].lower()

    if command in ("/start", "/help", "/menu"):
        send_message(token, chat_id, _help_text(is_admin))
        return

    if command == "/stop":
        has_running_job = bool(_current_drivers.get(user_id)) or _stop_event_for(user_id).is_set()
        if not has_running_job:
            send_message(token, chat_id, "Bạn không có tiến trình /regget nào đang chạy.")
            return
        _request_stop_job(user_id)
        send_message(token, chat_id, "Đã gửi lệnh dừng tiến trình /regget hiện tại. Nếu trình duyệt đang bận, bot sẽ thoát ở điểm kiểm tra gần nhất.")
        return

    if command == "/done":
        has_running_job = bool(_current_drivers.get(user_id)) or _stop_event_for(user_id).is_set()
        if not has_running_job:
            send_message(token, chat_id, "Bạn không có profile đang chạy để đóng/xoá.")
            return
        _request_done_job(user_id)
        send_message(token, chat_id, "Đã đóng toàn bộ trình duyệt/profiles đang chạy và gửi lệnh xoá profile.")
        return

    if command == "/focus":
        raw_lines = [line.strip() for line in args.splitlines()]
        domains = [line.lstrip("@") for line in raw_lines if line.strip()]
        if not domains:
            send_message(token, chat_id, "Định dạng:\n/focus domain1\ndomain2\ndomain3")
            return
        try:
            focused = set_focus_domains(domains)
            send_message(
                token,
                chat_id,
                "Đã chuyển temp mail sang mode focus:\n" + "\n".join(f"@{item}" for item in focused),
            )
        except ValueError as e:
            send_message(token, chat_id, str(e))
        return

    if command == "/alldm":
        set_domain_mode_all()
        send_message(token, chat_id, f"Đã chuyển temp mail sang all-domain.\nMode hiện tại: {describe_domain_mode()}")
        return

    if command == "/blockdm":
        raw_lines = [line.strip() for line in args.splitlines()]
        domains = [line.lstrip("@") for line in raw_lines if line.strip()]
        if not domains:
            send_message(token, chat_id, "Định dạng:\n/blockdm domain1\ndomain2")
            return
        try:
            blocked = add_blocked_domains(domains)
            send_message(
                token,
                chat_id,
                "Đã thêm domain vào blacklist OTP:\n" + "\n".join(f"@{item}" for item in blocked),
            )
        except ValueError as e:
            send_message(token, chat_id, str(e))
        return

    if command == "/unblockdm":
        raw_lines = [line.strip() for line in args.splitlines()]
        domains = [line.lstrip("@") for line in raw_lines if line.strip()]
        if not domains:
            send_message(token, chat_id, "Định dạng:\n/unblockdm domain1\ndomain2")
            return
        remaining = remove_blocked_domains(domains)
        if remaining:
            send_message(
                token,
                chat_id,
                "Đã gỡ domain khỏi blacklist OTP.\nCòn lại:\n" + "\n".join(f"@{item}" for item in remaining),
            )
        else:
            send_message(token, chat_id, "Đã gỡ domain khỏi blacklist OTP. Hiện blacklist trống.")
        return

    if command == "/cleardm":
        clear_blocked_domains()
        send_message(token, chat_id, "Đã xoá toàn bộ blacklist domain OTP.")
        return

    if command == "/dmstatus":
        blocked = get_blocked_domains()
        message = [f"Mode hiện tại: {describe_domain_mode()}"]
        if blocked:
            message.append("Blacklist OTP:")
            message.extend(f"@{item}" for item in blocked)
        else:
            message.append("Blacklist OTP: trống")
        send_message(token, chat_id, "\n".join(message))
        return

    if command == "/pass":
        if not is_admin:
            send_message(token, chat_id, "Chỉ admin được đặt pass mặc định.")
            return
        password = args.strip()
        if not password:
            current = _default_password()
            send_message(token, chat_id, f"Pass mặc định hiện tại: {current or 'chưa đặt'}")
            return
        _set_default_password(password)
        send_message(token, chat_id, "Đã đặt pass mặc định.")
        return

    if command == "/ban":
        if not is_admin:
            send_message(token, chat_id, "Chỉ admin được ban.")
            return
        try:
            target = _ban_target(args)
            send_message(token, chat_id, f"Đã ban {target}.")
        except ValueError as e:
            send_message(token, chat_id, str(e))
        return

    if command == "/addcre":
        if not is_admin:
            send_message(token, chat_id, "Chỉ admin được cấp credit.")
            return
        parts = args.split()
        if len(parts) != 2:
            send_message(token, chat_id, "Định dạng: /addcre username_or_id số_credit")
            return
        try:
            added = int(parts[1])
            result = _add_credit(parts[0], added)
            send_message(token, chat_id, f"Đã cấp credit cho {result}.")
        except ValueError as e:
            send_message(token, chat_id, str(e))
        return

    if command == "/addluong":
        if not is_admin:
            send_message(token, chat_id, "Chỉ admin được mở luồng.")
            return
        target = args.strip()
        if not target:
            send_message(token, chat_id, "Định dạng: /addluong username_or_id")
            return
        try:
            result = _add_thread_limit(target)
            send_message(token, chat_id, f"Đã mở luồng cho {result}.")
        except ValueError as e:
            send_message(token, chat_id, str(e))
        return

    if command == "/unban":
        if not is_admin:
            send_message(token, chat_id, "Chỉ admin được unban.")
            return
        try:
            target = _unban_target(args)
            send_message(token, chat_id, f"Đã unban {target}.")
        except ValueError as e:
            send_message(token, chat_id, str(e))
        return

    if command == "/regget":
        try:
            payload = _clean_payload(text)
            if not payload and document:
                payload = _download_telegram_text_document(token, document).strip()
            accounts = _parse_bundles(payload)
        except ValueError as e:
            send_message(token, chat_id, f"Lỗi input: {e}")
            return
        except requests.RequestException as e:
            send_message(token, chat_id, f"Không tải được file txt từ Telegram: {e}")
            return

        if not accounts:
            send_message(token, chat_id, "Thiếu cụm mail. Định dạng: email|mật_khẩu_mail|refresh_token|client_id")
            return
        if not is_admin and _get_credits(user_id) <= 0:
            send_message(token, chat_id, "Bạn đã hết credit. Vui lòng liên hệ admin để được cấp thêm.")
            return
        if not is_admin and len(accounts) > MAX_USER_BUNDLES:
            send_message(token, chat_id, "User chỉ được gửi tối đa 1 cụm mail mỗi lần.")
            return

        threading.Thread(
            target=_run_regget_job,
            args=(token, chat_id, user_id, username, accounts, is_admin),
            daemon=True,
        ).start()
        return

    if command in {"/regtmail4", "/regtmail2"}:
        quantity = 4 if command == "/regtmail4" else 2
        threading.Thread(
            target=_run_regtmail_job,
            args=(token, chat_id, user_id, username, is_admin, quantity),
            daemon=True,
        ).start()
        return

    if command == "/regtmail":
        quantity_text = args.strip() or "1"
        try:
            quantity = int(quantity_text)
        except ValueError:
            send_message(token, chat_id, "Định dạng: /regtmail số_lượng")
            return
        if quantity <= 0:
            send_message(token, chat_id, "Số lượng phải lớn hơn 0.")
            return
        threading.Thread(
            target=_run_regtmail_job,
            args=(token, chat_id, user_id, username, is_admin, quantity),
            daemon=True,
        ).start()
        return

    send_message(token, chat_id, "Không hiểu lệnh. Gửi /menu để xem hướng dẫn sử dụng.")


def run_bot() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    token_file = os.environ.get("TELEGRAM_BOT_TOKEN_FILE", "").strip()
    if not token and token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except Exception as e:
            raise SystemExit(f"Không đọc được TELEGRAM_BOT_TOKEN_FILE: {e}") from e
    if not token:
        raise SystemExit("Thiếu TELEGRAM_BOT_TOKEN hoặc TELEGRAM_BOT_TOKEN_FILE")

    if cleanup_gpm_profiles is not None:
        try:
            _log("Cleanup GPM profile chatgpt-auto* trước khi bot chạy")
            cleanup_gpm_profiles.main()
        except Exception as e:
            _log(f"Cleanup GPM khi khởi động thất bại: {e}")
    else:
        _log("Không có module cleanup_gpm_profiles, bỏ qua cleanup lúc khởi động")

    _set_bot_commands(token)

    offset = None
    print("Telegram bot started")
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            response = requests.get(_api_url(token, "getUpdates"), params=params, timeout=60)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                time.sleep(2)
                continue
            for update in data.get("result", []):
                offset = int(update["update_id"]) + 1
                try:
                    handle_update(token, update)
                except Exception as e:
                    print(f"Handle update error: {e}")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"Telegram polling error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run_bot()
