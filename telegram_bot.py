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
from urllib.parse import quote, unquote, urlsplit

import requests

import main
import browser
import bizmailer_checkout
import checkout_new
try:
    import cleanup_gpm_profiles
except ModuleNotFoundError:
    cleanup_gpm_profiles = None
from email_service import HotmailAccount, create_temp_email, wait_for_verification_email, snapshot_message_ids
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
MAX_REGGET_JOBS = 4
DEFAULT_USER_CREDITS = 10
JOB_STALL_TIMEOUT = int(os.environ.get("TELEGRAM_JOB_STALL_TIMEOUT", "900"))
REGGET_SINGLE_WINDOW_WIDTH = int(os.environ.get("REGGET_SINGLE_WINDOW_WIDTH", "1500"))
REGGET_SINGLE_WINDOW_HEIGHT = int(os.environ.get("REGGET_SINGLE_WINDOW_HEIGHT", "980"))

_state_lock = threading.Lock()
_user_locks: dict[int, threading.Lock] = {}
_user_slots: dict[int, threading.BoundedSemaphore] = {}
_user_locks_guard = threading.Lock()
_global_browser_slots = threading.BoundedSemaphore(MAX_GLOBAL_BROWSERS)
_regget_job_slots = threading.BoundedSemaphore(MAX_REGGET_JOBS)
_regget_queue_lock = threading.Lock()
_regget_waiting_jobs = 0
_stop_events: dict[int, threading.Event] = {}
_current_drivers: dict[int, list[Any]] = {}
_active_regget_jobs: dict[int, int] = {}
_driver_lock = threading.Lock()
_pending_gopay_otp: dict[int, dict[str, Any]] = {}
_pending_get_session_chunks: dict[int, dict[str, Any]] = {}
_pending_getstripe_chunks: dict[int, dict[str, Any]] = {}
_pending_covn_chunks: dict[int, dict[str, Any]] = {}


def _enable_large_single_window() -> None:
    browser.set_visible_grid_override(
        cols=1,
        rows=1,
        width=REGGET_SINGLE_WINDOW_WIDTH,
        height=REGGET_SINGLE_WINDOW_HEIGHT,
    )
    browser.set_profile_zoom_override(1.0)


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


def _latest_driver_for(user_id: int) -> Any | None:
    with _driver_lock:
        drivers = list(_current_drivers.get(user_id, []))
    return drivers[-1] if drivers else None


def _mark_regget_job_started(user_id: int) -> None:
    with _driver_lock:
        _active_regget_jobs[user_id] = int(_active_regget_jobs.get(user_id, 0)) + 1


def _mark_regget_job_finished(user_id: int) -> None:
    with _driver_lock:
        current = int(_active_regget_jobs.get(user_id, 0))
        if current <= 1:
            _active_regget_jobs.pop(user_id, None)
        else:
            _active_regget_jobs[user_id] = current - 1


def _has_active_regget_job(user_id: int) -> bool:
    with _driver_lock:
        return int(_active_regget_jobs.get(user_id, 0)) > 0


def _await_gopay_otp_from_telegram(
    token: str,
    chat_id: int,
    user_id: int,
    username: str = "",
    prompt: str = "",
    timeout: int = 180,
) -> str | None:
    event = threading.Event()
    with _driver_lock:
        _pending_gopay_otp[user_id] = {
            "event": event,
            "code": None,
            "chat_id": chat_id,
            "created_at": time.time(),
        }

    label = _user_label(user_id, username)
    ask = prompt.strip() or "Đã tới bước OTP GoPay."
    send_message(
        token,
        chat_id,
        f"{ask}\nGửi `/otp 123456` hoặc nhắn trực tiếp `123456` trong {timeout}s để bot nhập tiếp.",
    )
    _log(f"{label} | đang chờ OTP GoPay từ Telegram")

    ok = event.wait(timeout)
    with _driver_lock:
        pending = _pending_gopay_otp.pop(user_id, None) or {}
    code = str(pending.get("code") or "").strip()
    if ok and code:
        _log(f"{label} | đã nhận OTP GoPay từ Telegram")
        return code
    send_message(token, chat_id, "Hết thời gian chờ OTP GoPay hoặc chưa nhận được mã hợp lệ.")
    return None


def _submit_pending_gopay_otp(user_id: int, code: str) -> bool:
    with _driver_lock:
        pending = _pending_gopay_otp.get(user_id)
        if not pending:
            return False
        pending["code"] = str(code).strip()
        event = pending.get("event")
    if isinstance(event, threading.Event):
        event.set()
        return True
    return False


def _start_get_session_capture(user_id: int, chat_id: int) -> None:
    with _driver_lock:
        _pending_get_session_chunks[user_id] = {
            "chat_id": chat_id,
            "chunks": [],
            "created_at": time.time(),
        }


def _append_get_session_chunk(user_id: int, chat_id: int, text: str) -> bool:
    chunk = str(text or "")
    with _driver_lock:
        pending = _pending_get_session_chunks.get(user_id)
        if not pending or int(pending.get("chat_id") or 0) != int(chat_id):
            return False
        pending.setdefault("chunks", []).append(chunk)
        pending["updated_at"] = time.time()
        return True


def _get_session_capture_stats(user_id: int, chat_id: int) -> tuple[int, int]:
    with _driver_lock:
        pending = _pending_get_session_chunks.get(user_id)
        if not pending or int(pending.get("chat_id") or 0) != int(chat_id):
            return 0, 0
        chunks = list(pending.get("chunks") or [])
    return len(chunks), sum(len(item) for item in chunks)


def _peek_get_session_chunks(user_id: int, chat_id: int) -> str:
    with _driver_lock:
        pending = _pending_get_session_chunks.get(user_id)
        if not pending or int(pending.get("chat_id") or 0) != int(chat_id):
            return ""
        chunks = list(pending.get("chunks") or [])
    return "".join(chunks)


def _pop_get_session_chunks(user_id: int, chat_id: int) -> str:
    with _driver_lock:
        pending = _pending_get_session_chunks.get(user_id)
        if not pending or int(pending.get("chat_id") or 0) != int(chat_id):
            return ""
        _pending_get_session_chunks.pop(user_id, None)
    return "".join(list(pending.get("chunks") or []))


def _cancel_get_session_capture(user_id: int, chat_id: int) -> bool:
    with _driver_lock:
        pending = _pending_get_session_chunks.get(user_id)
        if not pending or int(pending.get("chat_id") or 0) != int(chat_id):
            return False
        _pending_get_session_chunks.pop(user_id, None)
        return True


def _has_pending_get_session_capture(user_id: int, chat_id: int) -> bool:
    with _driver_lock:
        pending = _pending_get_session_chunks.get(user_id)
        return bool(pending and int(pending.get("chat_id") or 0) == int(chat_id))


def _prepare_session_json_payload(raw_payload: str) -> str:
    payload_text = str(raw_payload or "").strip()
    if payload_text.startswith("```"):
        payload_text = re.sub(r"^```(?:json)?\s*", "", payload_text, flags=re.IGNORECASE)
        payload_text = re.sub(r"\s*```$", "", payload_text)
        payload_text = payload_text.strip()
    first_brace = payload_text.find("{")
    last_brace = payload_text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        payload_text = payload_text[first_brace:last_brace + 1]
    return payload_text


def _looks_like_complete_session_json(raw_payload: str) -> bool:
    payload_text = _prepare_session_json_payload(raw_payload)
    if not payload_text:
        return False
    try:
        parsed = json.loads(payload_text)
    except Exception:
        return False
    return isinstance(parsed, dict) and bool(parsed)


def _start_getstripe_capture(user_id: int, chat_id: int) -> None:
    with _driver_lock:
        _pending_getstripe_chunks[user_id] = {
            "chat_id": chat_id,
            "chunks": [],
            "created_at": time.time(),
        }


def _append_getstripe_chunk(user_id: int, chat_id: int, text: str) -> bool:
    chunk = str(text or "")
    with _driver_lock:
        pending = _pending_getstripe_chunks.get(user_id)
        if not pending or int(pending.get("chat_id") or 0) != int(chat_id):
            return False
        pending.setdefault("chunks", []).append(chunk)
        pending["updated_at"] = time.time()
        return True


def _get_getstripe_stats(user_id: int, chat_id: int) -> tuple[int, int]:
    with _driver_lock:
        pending = _pending_getstripe_chunks.get(user_id)
        if not pending or int(pending.get("chat_id") or 0) != int(chat_id):
            return 0, 0
        chunks = list(pending.get("chunks") or [])
    return len(chunks), sum(len(item) for item in chunks)


def _peek_getstripe_chunks(user_id: int, chat_id: int) -> str:
    with _driver_lock:
        pending = _pending_getstripe_chunks.get(user_id)
        if not pending or int(pending.get("chat_id") or 0) != int(chat_id):
            return ""
        chunks = list(pending.get("chunks") or [])
    return "".join(chunks)


def _pop_getstripe_chunks(user_id: int, chat_id: int) -> str:
    with _driver_lock:
        pending = _pending_getstripe_chunks.get(user_id)
        if not pending or int(pending.get("chat_id") or 0) != int(chat_id):
            return ""
        _pending_getstripe_chunks.pop(user_id, None)
    return "".join(list(pending.get("chunks") or []))


def _cancel_getstripe_capture(user_id: int, chat_id: int) -> bool:
    with _driver_lock:
        pending = _pending_getstripe_chunks.get(user_id)
        if not pending or int(pending.get("chat_id") or 0) != int(chat_id):
            return False
        _pending_getstripe_chunks.pop(user_id, None)
        return True


def _has_pending_getstripe_capture(user_id: int, chat_id: int) -> bool:
    with _driver_lock:
        pending = _pending_getstripe_chunks.get(user_id)
        return bool(pending and int(pending.get("chat_id") or 0) == int(chat_id))


def _start_covn_capture(user_id: int, chat_id: int) -> None:
    with _driver_lock:
        _pending_covn_chunks[user_id] = {
            "chat_id": chat_id,
            "chunks": [],
            "created_at": time.time(),
        }


def _append_covn_chunk(user_id: int, chat_id: int, text: str) -> bool:
    chunk = str(text or "")
    with _driver_lock:
        pending = _pending_covn_chunks.get(user_id)
        if not pending or int(pending.get("chat_id") or 0) != int(chat_id):
            return False
        pending.setdefault("chunks", []).append(chunk)
        pending["updated_at"] = time.time()
        return True


def _peek_covn_chunks(user_id: int, chat_id: int) -> str:
    with _driver_lock:
        pending = _pending_covn_chunks.get(user_id)
        if not pending or int(pending.get("chat_id") or 0) != int(chat_id):
            return ""
        chunks = list(pending.get("chunks") or [])
    return "".join(chunks)


def _pop_covn_chunks(user_id: int, chat_id: int) -> str:
    with _driver_lock:
        pending = _pending_covn_chunks.get(user_id)
        if not pending or int(pending.get("chat_id") or 0) != int(chat_id):
            return ""
        _pending_covn_chunks.pop(user_id, None)
    return "".join(list(pending.get("chunks") or []))


def _cancel_covn_capture(user_id: int, chat_id: int) -> bool:
    with _driver_lock:
        pending = _pending_covn_chunks.get(user_id)
        if not pending or int(pending.get("chat_id") or 0) != int(chat_id):
            return False
        _pending_covn_chunks.pop(user_id, None)
        return True


def _has_pending_covn_capture(user_id: int, chat_id: int) -> bool:
    with _driver_lock:
        pending = _pending_covn_chunks.get(user_id)
        return bool(pending and int(pending.get("chat_id") or 0) == int(chat_id))


def _close_user_drivers(user_id: int, reason: str = "") -> bool:
    with _driver_lock:
        drivers = list(_current_drivers.get(user_id, []))

    closed_any = False
    for driver in drivers:
        try:
            driver.quit()
            closed_any = True
        except Exception as e:
            suffix = f" ({reason})" if reason else ""
            _log(f"Đóng trình duyệt thất bại{suffix}: {e}")

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
    _clear_driver(user_id)
    return event.is_set() or closed


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "admin_ids": [],
            "banned_user_ids": [],
            "banned_usernames": [],
            "tutorial_text": "",
            "default_password": "",
            "user_credits": {},
            "user_threads": {},
            "user_canva_proxies": {},
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    data.setdefault("admin_ids", [])
    data.setdefault("banned_user_ids", [])
    data.setdefault("banned_usernames", [])
    data.setdefault("tutorial_text", "")
    data.setdefault("default_password", "")
    data.setdefault("user_credits", {})
    data.setdefault("user_threads", {})
    data.setdefault("user_canva_proxies", {})
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


def _ensure_credit_available(token: str, chat_id: int, user_id: int, is_admin: bool) -> bool:
    if is_admin:
        return True
    current = _get_credits(user_id)
    if current > 0:
        return True
    send_message(token, chat_id, "Bạn đã hết credit. Vui lòng liên hệ admin để được cấp thêm.")
    return False


def _consume_success_credit(
    user_id: int,
    is_admin: bool,
    credit_state: dict[str, bool] | None = None,
) -> int | None:
    if is_admin:
        return None
    if credit_state is not None:
        if credit_state.get("charged"):
            return None
        credit_state["charged"] = True
    return _consume_credit(user_id, 1)


def _acquire_regget_job_slot(token: str, chat_id: int) -> None:
    global _regget_waiting_jobs
    if _regget_job_slots.acquire(blocking=False):
        return

    with _regget_queue_lock:
        _regget_waiting_jobs += 1
        queue_position = _regget_waiting_jobs

    send_message(
        token,
        chat_id,
        f"Hệ thống đang chạy đủ {MAX_REGGET_JOBS} job /regget. Job của bạn đang xếp hàng vị trí {queue_position}.",
    )
    _regget_job_slots.acquire()
    with _regget_queue_lock:
        _regget_waiting_jobs = max(0, _regget_waiting_jobs - 1)
    send_message(token, chat_id, "Đã tới lượt job /regget của bạn, bắt đầu chạy.")


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


def _normalize_proxy_input(raw: str) -> dict[str, str]:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("Thiếu proxy. Ví dụ: /addprx host:port:user:pass")

    if value.lower() in {"off", "none", "clear", "xoa", "xoá"}:
        return {"normalized": "", "masked": "đã tắt"}

    candidate = value
    if "://" not in candidate:
        parts = candidate.split(":")
        if "@" in candidate:
            candidate = f"http://{candidate}"
        elif len(parts) == 2 and parts[1].isdigit():
            host, port = parts
            candidate = f"http://{host}:{port}"
        elif len(parts) == 4:
            if parts[1].isdigit():
                host, port, user, password = parts
            elif parts[3].isdigit():
                user, password, host, port = parts
            else:
                raise ValueError("Proxy 4 phần không hợp lệ. Dùng host:port:user:pass hoặc user:pass:host:port")
            candidate = f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
        else:
            raise ValueError(
                "Proxy không hợp lệ. Hỗ trợ: host:port | host:port:user:pass | user:pass@host:port | scheme://user:pass@host:port"
            )

    parsed = urlsplit(candidate)
    scheme = (parsed.scheme or "http").lower()
    if scheme not in {"http", "https", "socks5", "socks4"}:
        raise ValueError("Scheme proxy chỉ hỗ trợ: http, https, socks5, socks4")
    host = parsed.hostname or ""
    port = parsed.port
    if not host or not port:
        raise ValueError("Proxy phải có host và port")

    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    auth = ""
    masked_auth = ""
    if username:
        auth = quote(username, safe="")
        masked_auth = username
        if password:
            auth += f":{quote(password, safe='')}"
            masked_auth += ":***"
        auth += "@"
        masked_auth += "@"

    normalized = f"{scheme}://{auth}{host}:{port}"
    masked = f"{scheme}://{masked_auth}{host}:{port}"
    return {"normalized": normalized, "masked": masked}


def _set_user_canva_proxy(user_id: int, raw_proxy: str) -> str:
    proxy = _normalize_proxy_input(raw_proxy)
    with _state_lock:
        state = _load_state()
        proxies = state.setdefault("user_canva_proxies", {})
        key = str(user_id)
        if proxy["normalized"]:
            proxies[key] = proxy["normalized"]
        else:
            proxies.pop(key, None)
        _save_state(state)
    return proxy["masked"]


def _get_user_canva_proxy(user_id: int) -> str:
    with _state_lock:
        state = _load_state()
        return str(state.setdefault("user_canva_proxies", {}).get(str(user_id), "") or "").strip()


def _mask_proxy_for_display(proxy: str) -> str:
    if not proxy:
        return "chưa cài"
    try:
        return _normalize_proxy_input(proxy)["masked"]
    except Exception:
        return proxy


def _set_default_password(password: str) -> None:
    with _state_lock:
        state = _load_state()
        state["default_password"] = password
        _save_state(state)


def _tutorial_text() -> str:
    with _state_lock:
        return str(_load_state().get("tutorial_text") or "").strip()


def _set_tutorial_text(text: str) -> None:
    with _state_lock:
        state = _load_state()
        state["tutorial_text"] = str(text or "").strip()
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
    if file_name and not file_name.lower().endswith((".txt", ".json")):
        raise ValueError("File phải là .txt hoặc .json")
    if mime_type and mime_type not in {"text/plain", "application/octet-stream", "application/json"}:
        raise ValueError(f"File text/json không hợp lệ, mime_type={mime_type}")

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
            self.last_activity = time.time()
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
    email = str(getattr(account, "email", "") or result.get("email") or "").strip()
    password = str(result.get("password") or "").strip()
    if not password:
        password = _default_password() or getattr(main, "DEFAULT_ACCOUNT_PASSWORD", "") or ""
    password = str(password).strip()
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
        if pay_link:
            return (
                f"MANUAL_CHECKOUT\n"
                f"{_result_bundle(result, account)}\n"
                f"{pay_link}\n"
                "Đã mở trang checkout, giữ trình duyệt để thanh toán tay"
            )
        return f"MANUAL_CHECKOUT\n{_result_bundle(result, account)}\nĐã mở trang checkout, giữ trình duyệt để thanh toán tay"
    if result.get("success") and pay_link:
        return f"{_result_bundle(result, account)}\n{pay_link}"
    if result.get("success") and result.get("trial_success"):
        return f"TRIAL_FREE\n{_result_bundle(result, account)}\nĐã tạo tài khoản và chạy trial_free thành công"
    if result.get("success") and result.get("no_trial"):
        return f"NO_TRIAL\n{_result_bundle(result, account)}\nĐã tạo tài khoản nhưng account không có trial"
    if pay_link:
        return f"FAIL\n{_result_bundle(result, account)}\n{pay_link}\n{reason}"
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
    credit_state: dict[str, bool] | None = None,
) -> None:
    results.append((account, result))
    if result.get("success") and (result.get("checkout_url") or result.get("trial_success") or result.get("manual_checkout_ready")):
        if result.get("manual_checkout_ready"):
            done_label = "đã mở checkout tay"
        else:
            done_label = "đã chạy trial" if result.get("trial_success") else "đã có link pay"
        progress.set(100, f"Hoàn tất {account.email}: {done_label}")
        remaining = _consume_success_credit(user_id, is_admin, credit_state)
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
    gopay_otp_callback=None,
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
                gopay_otp_callback=gopay_otp_callback,
            )
            return result
        finally:
            keep_driver_handle = bool(
                result and (
                    result.get("manual_checkout_ready")
                    or getattr(main, "KEEP_PROFILE_OPEN_FOR_DEBUG", False)
                )
            )
            if not keep_driver_handle:
                _clear_driver(user_id)


def _run_co_job(
    token: str,
    chat_id: int,
    user_id: int,
    username: str,
    checkout_url: str,
    is_admin: bool,
) -> None:
    if not _ensure_credit_available(token, chat_id, user_id, is_admin):
        return
    slot = _user_slot(user_id, is_admin=is_admin)
    if not slot.acquire(blocking=False):
        limit = _thread_limit_for(user_id, is_admin=is_admin)
        send_message(token, chat_id, f"Bạn đang chạy đủ {limit} luồng. Chờ một job xong rồi gửi tiếp.")
        return

    driver = None
    try:
        stop_event = _stop_event_for(user_id)
        stop_event.clear()
        with _global_browser_slots:
            _enable_large_single_window()
            send_message(token, chat_id, "Đã nhận lệnh test pay, đang mở browser để chạy flow từ link có sẵn...")
            driver = browser.create_driver(force_plain=True)
            _register_driver(user_id, driver)
            otp_callback = lambda prompt="Đã tới bước OTP GoPay.": _await_gopay_otp_from_telegram(
                token, chat_id, user_id, username, prompt
            )
            result = browser.complete_gopay_checkout_and_capture_redirect(
                driver,
                checkout_url,
                log_func=_log,
                otp_callback=otp_callback,
            )

            if result.get("success"):
                remaining = _consume_success_credit(user_id, is_admin)
                redirect_url = str(result.get("redirect_url") or checkout_url).strip()
                send_message(
                    token,
                    chat_id,
                    "CO_OK\n"
                    f"{redirect_url}\n"
                    "Đã chạy xong flow test pay."
                    + (f"\nCredit còn lại: {remaining}" if remaining is not None else ""),
                )
            else:
                reason = str(result.get("reason") or "Flow test pay thất bại").strip()
                current_url = ""
                if driver:
                    try:
                        current_url = str(driver.current_url or "").strip()
                    except Exception:
                        current_url = ""
                extra = f"\nURL hiện tại: {current_url}" if current_url else ""
                send_message(token, chat_id, f"CO_FAIL\n{reason}{extra}")
    except Exception as e:
        send_message(token, chat_id, f"CO_FAIL\n{e}")
    finally:
        keep_open = bool(main.KEEP_PROFILE_OPEN_FOR_DEBUG)
        if driver and not keep_open:
            try:
                driver.quit()
            except Exception:
                pass
        elif driver and keep_open:
            send_message(token, chat_id, "Debug mode đang bật, giữ nguyên browser/profile để bạn kiểm tra.")
        if driver:
            _clear_driver(user_id, driver)
        try:
            browser.set_visible_grid_override()
            browser.set_profile_zoom_override()
        except Exception:
            pass
        slot.release()


def _run_get_session_job(
    token: str,
    chat_id: int,
    user_id: int,
    username: str,
    is_admin: bool,
) -> None:
    driver = _latest_driver_for(user_id)
    if not driver:
        send_message(token, chat_id, "Không thấy browser/profile nào đang mở để lấy session. Hãy chạy job trước hoặc bật debug mode giữ profile.")
        return

    try:
        send_message(token, chat_id, "🔐 Đang lấy auth session hiện tại và gọi Bizmailer...")
        result = bizmailer_checkout.create_trial_checkout_via_bizmailer(driver, log_func=_log)
        if result.get("success"):
            checkout_url = str(result.get("checkout_url") or "").strip()
            send_message(token, chat_id, f"🎉 GET_OK\n🔗 {checkout_url}")
        else:
            reason = str(result.get("failure_reason") or "Lấy session/Bizmailer thất bại").strip()
            send_message(token, chat_id, f"❌ GET_FAIL\n{reason}")
    except Exception as e:
        send_message(token, chat_id, f"❌ GET_FAIL\n{e}")


def _run_get_raw_session_job(
    token: str,
    chat_id: int,
    user_id: int,
    is_admin: bool,
    raw_payload: str,
) -> None:
    try:
        payload_text = str(raw_payload or "").strip()
        if not payload_text:
            send_message(token, chat_id, "❌ GET_FAIL\nThiếu raw session JSON")
            return
        send_message(token, chat_id, f"📥 Đã nhận session. Độ dài thô: {len(payload_text)} ký tự. Đang kiểm tra JSON...")
        payload_text = _prepare_session_json_payload(payload_text)
        try:
            parsed = json.loads(payload_text)
        except Exception as e:
            hint = ""
            if "Unterminated string" in str(e):
                hint = "\nCó vẻ bạn đang thiếu một đoạn session ở giữa hoặc chưa gửi phần cuối JSON."
            elif "Expecting ',' delimiter" in str(e):
                hint = "\nCó vẻ session bị thiếu dấu phẩy hoặc bị mất một đoạn khi dán nhiều tin nhắn. Nếu bạn dán một tin quá dài, Telegram có thể đã cắt bớt nội dung."
            elif "Expecting value" in str(e):
                hint = "\nCó vẻ phần đầu session chưa bắt đầu đúng từ ký tự `{`."
            send_message(token, chat_id, f"❌ GET_FAIL\nSession JSON không hợp lệ: {e}{hint}")
            return
        send_message(token, chat_id, "🧩 Đã parse session JSON. Đang gọi Bizmailer...")
        result = bizmailer_checkout.create_trial_checkout_from_bizmailer_context(
            {"raw_data": json.dumps(parsed, ensure_ascii=False)},
            log_func=_log,
        )
        if result.get("success"):
            checkout_url = str(result.get("checkout_url") or "").strip()
            send_message(token, chat_id, f"🎉 GET_OK\n🔗 {checkout_url}")
        else:
            reason = str(result.get("failure_reason") or "Bizmailer thất bại").strip()
            send_message(token, chat_id, f"❌ GET_FAIL\n{reason}")
    except Exception as e:
        send_message(token, chat_id, f"❌ GET_FAIL\n{e}")


def _run_getstripe_session_job(
    token: str,
    chat_id: int,
    user_id: int,
    is_admin: bool,
) -> None:
    driver = _latest_driver_for(user_id)
    if not driver:
        send_message(token, chat_id, "❌ GETSTRIPE_FAIL\nKhông thấy browser/profile nào đang mở để lấy session.")
        return
    try:
        send_message(token, chat_id, "🔐 Đã nhận yêu cầu getstripe từ browser hiện tại. Đang lấy auth context...")
        auth_context = checkout_new.extract_checkout_auth_context(driver, log_func=_log)
        send_message(token, chat_id, "🧩 Đã lấy auth context. Đang gọi checkout_new...")
        result = checkout_new.create_trial_checkout_from_auth_context(auth_context, country_code="ID", currency="IDR", log_func=_log)
        if result.get("success"):
            send_message(token, chat_id, f"🎉 GETSTRIPE_OK\n🔗 {str(result.get('checkout_url') or '').strip()}")
        else:
            send_message(token, chat_id, f"❌ GETSTRIPE_FAIL\n{str(result.get('failure_reason') or 'checkout_new thất bại').strip()}")
    except Exception as e:
        send_message(token, chat_id, f"❌ GETSTRIPE_FAIL\n{e}")


def _run_getstripe_raw_session_job(
    token: str,
    chat_id: int,
    user_id: int,
    is_admin: bool,
    raw_payload: str,
) -> None:
    try:
        payload_text = str(raw_payload or "").strip()
        if not payload_text:
            send_message(token, chat_id, "❌ GETSTRIPE_FAIL\nThiếu raw session JSON")
            return
        send_message(token, chat_id, f"📥 Đã nhận session cho getstripe. Độ dài thô: {len(payload_text)} ký tự. Đang kiểm tra JSON...")
        payload_text = _prepare_session_json_payload(payload_text)
        try:
            parsed = json.loads(payload_text)
        except Exception as e:
            send_message(token, chat_id, f"❌ GETSTRIPE_FAIL\nSession JSON không hợp lệ: {e}")
            return
        token_value = str(checkout_new._extract_bearer_token(parsed) or "").strip()
        if not token_value:
            send_message(token, chat_id, "❌ GETSTRIPE_FAIL\nKhông tìm thấy access token trong session JSON")
            return
        session_token = str(parsed.get("sessionToken") or "").strip() if isinstance(parsed, dict) else ""
        synthetic_cookies: list[dict[str, Any]] = []
        if session_token:
            synthetic_cookies.extend([
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": session_token,
                    "domain": "chatgpt.com",
                    "path": "/",
                },
                {
                    "name": "next-auth.session-token",
                    "value": session_token,
                    "domain": "chatgpt.com",
                    "path": "/",
                },
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": session_token,
                    "domain": ".chatgpt.com",
                    "path": "/",
                },
                {
                    "name": "next-auth.session-token",
                    "value": session_token,
                    "domain": ".chatgpt.com",
                    "path": "/",
                },
                {
                    "name": "__Secure-authjs.session-token",
                    "value": session_token,
                    "domain": "chatgpt.com",
                    "path": "/",
                },
                {
                    "name": "authjs.session-token",
                    "value": session_token,
                    "domain": "chatgpt.com",
                    "path": "/",
                },
                {
                    "name": "__Secure-authjs.session-token",
                    "value": session_token,
                    "domain": ".chatgpt.com",
                    "path": "/",
                },
                {
                    "name": "authjs.session-token",
                    "value": session_token,
                    "domain": ".chatgpt.com",
                    "path": "/",
                },
            ])
        send_message(token, chat_id, "🧩 Đã parse session JSON cho getstripe. Đang gọi checkout_new...")
        result = checkout_new.create_trial_checkout_from_auth_context(
            {
                "token": token_value,
                "cookies": synthetic_cookies,
                "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            },
            country_code="ID",
            currency="IDR",
            log_func=_log,
        )
        if result.get("success"):
            send_message(token, chat_id, f"🎉 GETSTRIPE_OK\n🔗 {str(result.get('checkout_url') or '').strip()}")
        else:
            send_message(token, chat_id, f"❌ GETSTRIPE_FAIL\n{str(result.get('failure_reason') or 'checkout_new thất bại').strip()}")
    except Exception as e:
        send_message(token, chat_id, f"❌ GETSTRIPE_FAIL\n{e}")


def _run_covn_job(
    token: str,
    chat_id: int,
    user_id: int,
    username: str,
    is_admin: bool,
) -> None:
    driver = _latest_driver_for(user_id)
    if not driver:
        send_message(token, chat_id, "❌ COVN_FAIL\nKhông thấy browser/profile nào đang mở để lấy session.")
        return

    try:
        send_message(token, chat_id, "🇻🇳🔐 Đang lấy auth session hiện tại để tạo checkout VN...")
        result = checkout_new.create_vn_trial_checkout(driver, log_func=_log)
        if result.get("success"):
            checkout_url = str(result.get("checkout_url") or "").strip()
            send_message(token, chat_id, f"🇻🇳🎉 COVN_OK\n🔗 {checkout_url}")
        else:
            reason = str(result.get("failure_reason") or "Tạo checkout VN thất bại").strip()
            send_message(token, chat_id, f"❌ COVN_FAIL\n{reason}")
    except Exception as e:
        send_message(token, chat_id, f"❌ COVN_FAIL\n{e}")


def _run_covn_raw_session_job(
    token: str,
    chat_id: int,
    user_id: int,
    is_admin: bool,
    raw_payload: str,
) -> None:
    try:
        payload_text = str(raw_payload or "").strip()
        if not payload_text:
            send_message(token, chat_id, "❌ COVN_FAIL\nThiếu raw session JSON")
            return
        send_message(token, chat_id, f"📥 Đã nhận session cho covn. Độ dài thô: {len(payload_text)} ký tự. Đang kiểm tra JSON...")
        payload_text = _prepare_session_json_payload(payload_text)
        try:
            parsed = json.loads(payload_text)
        except Exception as e:
            send_message(token, chat_id, f"❌ COVN_FAIL\nSession JSON không hợp lệ: {e}")
            return
        token_value = str(checkout_new._extract_bearer_token(parsed) or "").strip()
        if not token_value:
            send_message(token, chat_id, "❌ COVN_FAIL\nKhông tìm thấy access token trong session JSON")
            return
        session_token = str(parsed.get("sessionToken") or "").strip() if isinstance(parsed, dict) else ""
        synthetic_cookies: list[dict[str, Any]] = []
        if session_token:
            synthetic_cookies.extend([
                {"name": "__Secure-next-auth.session-token", "value": session_token, "domain": "chatgpt.com", "path": "/"},
                {"name": "next-auth.session-token", "value": session_token, "domain": "chatgpt.com", "path": "/"},
                {"name": "__Secure-next-auth.session-token", "value": session_token, "domain": ".chatgpt.com", "path": "/"},
                {"name": "next-auth.session-token", "value": session_token, "domain": ".chatgpt.com", "path": "/"},
                {"name": "__Secure-authjs.session-token", "value": session_token, "domain": "chatgpt.com", "path": "/"},
                {"name": "authjs.session-token", "value": session_token, "domain": "chatgpt.com", "path": "/"},
                {"name": "__Secure-authjs.session-token", "value": session_token, "domain": ".chatgpt.com", "path": "/"},
                {"name": "authjs.session-token", "value": session_token, "domain": ".chatgpt.com", "path": "/"},
            ])
        send_message(token, chat_id, "🧩 Đã parse session JSON cho covn. Đang gọi checkout VN...")
        result = checkout_new.create_vn_trial_checkout_from_auth_context(
            {
                "token": token_value,
                "cookies": synthetic_cookies,
                "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            },
            log_func=_log,
        )
        if result.get("success"):
            send_message(token, chat_id, f"🇻🇳🎉 COVN_OK\n🔗 {str(result.get('checkout_url') or '').strip()}")
        else:
            send_message(token, chat_id, f"❌ COVN_FAIL\n{str(result.get('failure_reason') or 'checkout VN thất bại').strip()}")
    except Exception as e:
        send_message(token, chat_id, f"❌ COVN_FAIL\n{e}")


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

    regget_slot_acquired = False
    try:
        _mark_regget_job_started(user_id)
        _acquire_regget_job_slot(token, chat_id)
        regget_slot_acquired = True
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
        credit_state = {"charged": False}
        single_account_large_window = len(accounts) == 1
        if single_account_large_window:
            _enable_large_single_window()
            progress.log(
                f"Chỉ có 1 mail, mở trình duyệt kích thước lớn {REGGET_SINGLE_WINDOW_WIDTH}x{REGGET_SINGLE_WINDOW_HEIGHT} để debug 2FA",
                force=True,
            )

        if is_admin and len(accounts) > 1:
            with ThreadPoolExecutor(max_workers=min(MAX_ADMIN_PARALLEL, len(accounts))) as executor:
                future_map = {
                    executor.submit(
                        _run_one_account,
                        account,
                        user_id,
                        stop_event,
                        progress,
                        lambda prompt="Đã tới bước OTP GoPay.": _await_gopay_otp_from_telegram(
                            token, chat_id, user_id, username, prompt
                        ),
                    ): account
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
                    _finish_one_account(token, chat_id, user_id, username, account, result, is_admin, progress, results, credit_state)
        else:
            for account in accounts:
                if stop_event.is_set():
                    progress.log("Job đã được dừng trước khi chạy tài khoản tiếp theo", force=True)
                    break
                progress.log(f"Đang chạy tài khoản: {account.email}", force=True)
                try:
                    otp_callback = lambda prompt="Đã tới bước OTP GoPay.": _await_gopay_otp_from_telegram(
                        token, chat_id, user_id, username, prompt
                    )
                    result = _run_one_account(account, user_id, stop_event, progress, otp_callback)
                except InterruptedError:
                    progress.set(progress.percent, f"Đã dừng {account.email}")
                    result = {"success": False, "failure_reason": "Đã dừng theo lệnh /stop", "checkout_url": ""}
                except Exception as e:
                    progress.log(f"Lỗi khi xử lý {account.email}: {e}", force=True)
                    result = {"success": False, "failure_reason": str(e), "checkout_url": ""}
                _finish_one_account(token, chat_id, user_id, username, account, result, is_admin, progress, results, credit_state)
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
        if regget_slot_acquired:
            _regget_job_slots.release()
        _mark_regget_job_finished(user_id)
        slot.release()


def _run_regcv_job(
    token: str,
    chat_id: int,
    user_id: int,
    username: str,
    accounts: list[HotmailAccount],
    is_admin: bool,
) -> None:
    slot = _user_slot(user_id, is_admin=is_admin)
    if not slot.acquire(blocking=False):
        limit = _thread_limit_for(user_id, is_admin=is_admin)
        send_message(token, chat_id, f"Bạn đang chạy đủ {limit} luồng. Chờ một job xong rồi gửi tiếp.")
        return

    driver = None
    try:
        if len(accounts) <= 1:
            _enable_large_single_window()
        else:
            browser.set_visible_grid_override(cols=2, rows=1)
            browser.set_profile_zoom_override(1.0)
        canva_proxy = _get_user_canva_proxy(user_id)
        browser.set_gpm_raw_proxy_override(canva_proxy or None)
        stop_event = _stop_event_for(user_id)
        stop_event.clear()
        progress = TelegramProgress(token, chat_id, user_id, username)
        progress.set(1, "Đã nhận lệnh /regcv")
        if canva_proxy:
            progress.log(f"Canva sẽ dùng proxy: {_mask_proxy_for_display(canva_proxy)}", force=True)
        results: list[tuple[HotmailAccount, dict[str, Any]]] = []
        credit_state = {"charged": False}

        for account in accounts:
            if stop_event.is_set():
                break

            progress.set(10, f"Canva: bắt đầu {account.email}", force=True)
            driver = browser.create_driver(force_plain=True)
            _register_driver(user_id, driver)
            baseline_message_ids = snapshot_message_ids(account)

            def fetch_canva_otp(since_ts: float) -> str | None:
                code = wait_for_verification_email(
                    account,
                    timeout=90,
                    since_ts=since_ts,
                    baseline_message_ids=baseline_message_ids,
                )
                return str(code or "").strip() or None

            result = browser.complete_canva_email_signup_and_redeem(
                driver,
                account.email,
                otp_fetcher=fetch_canva_otp,
                promo_code="AFRICAGROW",
                log_func=_log,
            )

            if result.get("success"):
                progress.set(100, f"Hoàn tất Canva {account.email}", force=True)
                remaining = _consume_success_credit(user_id, is_admin, credit_state)
                send_message(
                    token,
                    chat_id,
                    "REGCV_OK\n"
                    f"{_bundle(account)}\n"
                    "Canva đã đăng ký + redeem AFRICAGROW + chọn MoMo"
                    + (f"\nCredit còn lại: {remaining}" if remaining is not None else ""),
                )
            else:
                progress.set(progress.percent, f"Canva fail {account.email}", force=True)
                send_message(
                    token,
                    chat_id,
                    "REGCV_FAIL\n"
                    f"{_bundle(account)}\n"
                    f"{str(result.get('reason') or 'Canva flow thất bại').strip()}",
                )

            results.append((account, result))

            keep_open = bool(getattr(main, "KEEP_PROFILE_OPEN_FOR_DEBUG", False))
            if driver and not keep_open:
                try:
                    driver.quit()
                except Exception:
                    pass
            elif driver and keep_open:
                send_message(token, chat_id, "Debug mode đang bật, giữ nguyên browser/profile Canva để bạn kiểm tra.")
            if driver:
                _clear_driver(user_id, driver)
                driver = None

        progress.flush()
        _notify_admins_regget_done(token, chat_id, user_id, username, results)
    except Exception as e:
        send_message(token, chat_id, f"REGCV_FAIL\n{e}")
    finally:
        browser.set_gpm_raw_proxy_override()
        browser.set_visible_grid_override()
        browser.set_profile_zoom_override()
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
            _clear_driver(user_id, driver)
        slot.release()


def _help_text(is_admin: bool) -> str:
    base = (
        "Menu hướng dẫn sử dụng bot\n\n"
        "/tut\n"
        "Xem nội dung hướng dẫn mà admin đã cài.\n\n"
        "/regget\n"
        "Dùng để đăng ký tài khoản và lấy link thanh toán.\n"
        "Gửi theo mẫu:\n"
        "/regget email|mật_khẩu_mail|refresh_token|client_id\n"
        "Hoặc gửi file .txt kèm caption /regget.\n\n"
        "/regcv\n"
        "Dùng để đăng ký Canva bằng Hotmail rồi redeem code AFRICAGROW.\n"
        "Gửi theo mẫu:\n"
        "/regcv email|mật_khẩu_mail|refresh_token|client_id\n"
        "Hoặc gửi file .txt kèm caption /regcv.\n\n"
        "/addprx\n"
        "Lưu proxy dùng riêng cho Canva.\n"
        "Ví dụ:\n"
        "/addprx host:port\n"
        "/addprx host:port:user:pass\n"
        "/addprx user:pass@host:port\n\n"
        "/checkprx\n"
        "Xem proxy Canva hiện tại.\n\n"
        "/get\n"
        "Dùng để đổi session sang link Midtrans.\n"
        "Có 3 cách:\n"
        "1. /get session\n"
        "2. /get rồi dán nhiều đoạn, xong gửi /getdone\n"
        "3. Gửi file .txt/.json kèm caption /get\n\n"
        "/covn\n"
        "Dùng để tạo checkout VN tối ưu Apple Pay.\n"
        "Có 3 cách:\n"
        "1. /covn session\n"
        "2. /covn rồi dán nhiều đoạn, xong gửi /covndone\n"
        "3. Gửi file .txt/.json kèm caption /covn\n\n"
        "/getstripe\n"
        "Dùng để đổi session sang checkout link của checkout_new.\n"
        "Có 3 cách:\n"
        "1. /getstripe session\n"
        "2. /getstripe rồi dán nhiều đoạn, xong gửi /getstripedone\n"
        "3. Gửi file .txt/.json kèm caption /getstripe\n\n"
        "Giới hạn user:\n"
        "- Mỗi user chạy tối đa 1 job cùng lúc.\n"
        "- Mỗi user mặc định có 10 credit.\n"
        "- Mỗi lệnh thành công trừ 1 credit.\n"
    )
    if not is_admin:
        return base
    return (
        base
        + "\nLệnh admin:\n"
        "/addtut nội_dung_hướng_dẫn\n"
        "/ban username_or_id\n"
        "/unban username_or_id\n"
        "/addcre username_or_id số_credit\n"
        "/addluong username_or_id\n"
    )


def _reject_non_admin_private_mode(token: str, chat_id: int) -> None:
    send_message(
        token,
        chat_id,
        "Bot đang ở chế độ private. Chỉ admin được phép sử dụng.",
    )


def _telegram_menu_commands() -> list[dict[str, str]]:
    return [
        {"command": "tut", "description": "Xem nội dung hướng dẫn đã cài"},
        {"command": "regget", "description": "Đăng ký và lấy link thanh toán"},
        {"command": "regcv", "description": "Đăng ký Canva + redeem AFRICAGROW"},
        {"command": "addprx", "description": "Lưu proxy cho Canva"},
        {"command": "checkprx", "description": "Xem proxy Canva hiện tại"},
        {"command": "get", "description": "Lấy session hiện tại -> link Midtrans"},
        {"command": "covn", "description": "Tạo checkout VN tối ưu Apple Pay"},
        {"command": "getstripe", "description": "Lấy session hiện tại -> checkout link"},
        {"command": "addtut", "description": "Admin cập nhật nội dung /tut"},
        {"command": "unban", "description": "Admin bỏ ban user"},
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

        if not _ensure_credit_available(token, chat_id, user_id, is_admin):
            return

        results: list[tuple[Any, dict[str, Any]]] = []
        created_accounts: list[Any] = []
        credit_state = {"charged": False}
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
                _enable_large_single_window()
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
                        _finish_one_account(token, chat_id, user_id, username, account, result, is_admin, progress, results, credit_state)
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
    _bootstrap_admin_if_needed(user_id)
    _sync_user_credit_identity(user_id, username)
    is_admin = _is_admin(user_id)
    text = str(message.get("text") or message.get("caption") or "").strip()
    if not text and document and _has_pending_get_session_capture(user_id, chat_id):
        try:
            payload = _download_telegram_text_document(token, document).strip()
        except Exception as e:
            send_message(token, chat_id, f"GET_FAIL\nKhông đọc được file session: {e}")
            return
        if not payload:
            send_message(token, chat_id, "GET_FAIL\nFile session rỗng")
            return
        _cancel_get_session_capture(user_id, chat_id)
        threading.Thread(
            target=_run_get_raw_session_job,
            args=(token, chat_id, user_id, is_admin, payload),
            daemon=True,
        ).start()
        return
    if not text and document and _has_pending_getstripe_capture(user_id, chat_id):
        try:
            payload = _download_telegram_text_document(token, document).strip()
        except Exception as e:
            send_message(token, chat_id, f"GETSTRIPE_FAIL\nKhông đọc được file session: {e}")
            return
        if not payload:
            send_message(token, chat_id, "GETSTRIPE_FAIL\nFile session rỗng")
            return
        _cancel_getstripe_capture(user_id, chat_id)
        threading.Thread(
            target=_run_getstripe_raw_session_job,
            args=(token, chat_id, user_id, is_admin, payload),
            daemon=True,
        ).start()
        return
    if not text and document and _has_pending_covn_capture(user_id, chat_id):
        try:
            payload = _download_telegram_text_document(token, document).strip()
        except Exception as e:
            send_message(token, chat_id, f"COVN_FAIL\nKhông đọc được file session: {e}")
            return
        if not payload:
            send_message(token, chat_id, "COVN_FAIL\nFile session rỗng")
            return
        _cancel_covn_capture(user_id, chat_id)
        threading.Thread(
            target=_run_covn_raw_session_job,
            args=(token, chat_id, user_id, is_admin, payload),
            daemon=True,
        ).start()
        return
    if not text:
        return

    if _is_banned(user_id, username) and not is_admin:
        send_message(token, chat_id, "Bạn đã bị ban.")
        return

    command_parts = text.split(maxsplit=1)
    command = command_parts[0] if command_parts else ""
    args = command_parts[1] if len(command_parts) > 1 else ""
    command = command.split("@", 1)[0].lower()

    if not is_admin:
        allowed_user_commands = {
            "/start",
            "/help",
            "/menu",
            "/tut",
            "/regget",
            "/regcv",
            "/addprx",
            "/checkprx",
            "/get",
            "/covn",
            "/getdone",
            "/getcancel",
            "/covn",
            "/covndone",
            "/covncancel",
            "/getstripe",
            "/getstripedone",
            "/getstripecancel",
        }
        if command and command.startswith("/") and command not in allowed_user_commands:
            send_message(
                token,
                chat_id,
                "User thường chỉ dùng được: /tut, /regget, /regcv, /addprx, /checkprx, /get, /covn, /getstripe. Gửi /menu để xem hướng dẫn.",
            )
            return

    otp_match = re.fullmatch(r"(?:/otp\s+)?(\d{4,8})", text, flags=re.IGNORECASE)
    if otp_match and _submit_pending_gopay_otp(user_id, otp_match.group(1)):
        send_message(token, chat_id, f"Đã nhận OTP GoPay: {otp_match.group(1)}")
        return

    if command in ("/start", "/help", "/menu"):
        send_message(token, chat_id, _help_text(is_admin))
        return

    if command == "/tut":
        tutorial = _tutorial_text()
        if tutorial:
            send_message(token, chat_id, tutorial)
        else:
            send_message(token, chat_id, "Hiện chưa có nội dung hướng dẫn. Admin có thể cài bằng /addtut.")
        return

    if command == "/addprx":
        try:
            masked = _set_user_canva_proxy(user_id, args)
        except ValueError as e:
            send_message(token, chat_id, f"Lỗi proxy: {e}")
            return
        if masked == "đã tắt":
            send_message(token, chat_id, "Đã tắt proxy Canva cho tài khoản của bạn.")
        else:
            send_message(token, chat_id, f"Đã lưu proxy Canva:\n{masked}")
        return

    if command == "/checkprx":
        proxy = _get_user_canva_proxy(user_id)
        if not proxy:
            send_message(token, chat_id, "Hiện bạn chưa cài proxy Canva. Dùng /addprx để thêm.")
        else:
            send_message(token, chat_id, f"Proxy Canva hiện tại:\n{_mask_proxy_for_display(proxy)}")
        return

    if command == "/getdone":
        payload = _pop_get_session_chunks(user_id, chat_id)
        if not payload:
            send_message(token, chat_id, "Không có phiên /get nào đang chờ ghép chuỗi.")
            return
        threading.Thread(
            target=_run_get_raw_session_job,
            args=(token, chat_id, user_id, is_admin, payload),
            daemon=True,
        ).start()
        return

    if command == "/getstripedone":
        payload = _pop_getstripe_chunks(user_id, chat_id)
        if not payload:
            send_message(token, chat_id, "Không có phiên /getstripe nào đang chờ ghép chuỗi.")
            return
        threading.Thread(
            target=_run_getstripe_raw_session_job,
            args=(token, chat_id, user_id, is_admin, payload),
            daemon=True,
        ).start()
        return

    if command == "/getcancel":
        if _cancel_get_session_capture(user_id, chat_id):
            send_message(token, chat_id, "Đã hủy chế độ dán session cho /get.")
        else:
            send_message(token, chat_id, "Không có chế độ /get nào đang chờ để hủy.")
        return

    if command == "/getstripecancel":
        if _cancel_getstripe_capture(user_id, chat_id):
            send_message(token, chat_id, "Đã hủy chế độ dán session cho /getstripe.")
        else:
            send_message(token, chat_id, "Không có chế độ /getstripe nào đang chờ để hủy.")
        return

    if command == "/covndone":
        payload = _pop_covn_chunks(user_id, chat_id)
        if not payload:
            send_message(token, chat_id, "Không có phiên /covn nào đang chờ ghép chuỗi.")
            return
        threading.Thread(
            target=_run_covn_raw_session_job,
            args=(token, chat_id, user_id, is_admin, payload),
            daemon=True,
        ).start()
        return

    if command == "/covncancel":
        if _cancel_covn_capture(user_id, chat_id):
            send_message(token, chat_id, "Đã hủy chế độ dán session cho /covn.")
        else:
            send_message(token, chat_id, "Không có chế độ /covn nào đang chờ để hủy.")
        return

    if _has_pending_get_session_capture(user_id, chat_id) and command not in {"/get"}:
        if _append_get_session_chunk(user_id, chat_id, text):
            preview_payload = _peek_get_session_chunks(user_id, chat_id)
            payload = _pop_get_session_chunks(user_id, chat_id) if _looks_like_complete_session_json(preview_payload) else ""
            if payload:
                threading.Thread(
                    target=_run_get_raw_session_job,
                    args=(token, chat_id, user_id, is_admin, payload),
                    daemon=True,
                ).start()
                return
            return
    if _has_pending_getstripe_capture(user_id, chat_id) and command not in {"/getstripe"}:
        if _append_getstripe_chunk(user_id, chat_id, text):
            preview_payload = _peek_getstripe_chunks(user_id, chat_id)
            payload = _pop_getstripe_chunks(user_id, chat_id) if _looks_like_complete_session_json(preview_payload) else ""
            if payload:
                threading.Thread(
                    target=_run_getstripe_raw_session_job,
                    args=(token, chat_id, user_id, is_admin, payload),
                    daemon=True,
                ).start()
                return
            return
    if _has_pending_covn_capture(user_id, chat_id) and command not in {"/covn"}:
        if _append_covn_chunk(user_id, chat_id, text):
            preview_payload = _peek_covn_chunks(user_id, chat_id)
            payload = _pop_covn_chunks(user_id, chat_id) if _looks_like_complete_session_json(preview_payload) else ""
            if payload:
                threading.Thread(
                    target=_run_covn_raw_session_job,
                    args=(token, chat_id, user_id, is_admin, payload),
                    daemon=True,
                ).start()
                return
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
        if _has_active_regget_job(user_id):
            send_message(
                token,
                chat_id,
                "Job /regget của bạn vẫn đang chạy hoặc còn account đang retry. "
                "Chờ job chạy xong rồi mới dùng /done, hoặc dùng /stop nếu muốn dừng hẳn job hiện tại.",
            )
            return
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
        password = args.strip()
        if not password:
            current = _default_password()
            send_message(token, chat_id, f"Pass mặc định hiện tại: {current or 'chưa đặt'}")
            return
        _set_default_password(password)
        send_message(token, chat_id, "Đã đặt pass mặc định.")
        return

    if command == "/addtut":
        tutorial = args.strip()
        if not tutorial:
            current = _tutorial_text()
            send_message(token, chat_id, f"Nội dung /tut hiện tại:\n{current or '(trống)'}")
            return
        _set_tutorial_text(tutorial)
        send_message(token, chat_id, "Đã cập nhật nội dung /tut.")
        return

    if command == "/ban":
        try:
            target = _ban_target(args)
            send_message(token, chat_id, f"Đã ban {target}.")
        except ValueError as e:
            send_message(token, chat_id, str(e))
        return

    if command == "/addcre":
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
        if not _ensure_credit_available(token, chat_id, user_id, is_admin):
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

    if command == "/regcv":
        try:
            payload = re.sub(r"^/regcv(?:@\w+)?", "", text, count=1).strip().replace("```", "").strip()
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
        if not _ensure_credit_available(token, chat_id, user_id, is_admin):
            return
        if not is_admin and len(accounts) > MAX_USER_BUNDLES:
            send_message(token, chat_id, "User chỉ được gửi tối đa 1 cụm mail mỗi lần.")
            return

        threading.Thread(
            target=_run_regcv_job,
            args=(token, chat_id, user_id, username, accounts, is_admin),
            daemon=True,
        ).start()
        return

    if command in {"/co", "/pay"}:
        if not _ensure_credit_available(token, chat_id, user_id, is_admin):
            return
        checkout_url = args.strip()
        if not checkout_url:
            if command == "/pay":
                send_message(token, chat_id, "Định dạng: /pay https://app.midtrans.com/...")
            else:
                send_message(token, chat_id, "Định dạng: /co https://pay.openai.com/... hoặc https://app.midtrans.com/...")
            return
        if command == "/pay":
            if not re.match(r"^https://app\.midtrans\.com/", checkout_url, flags=re.IGNORECASE):
                send_message(token, chat_id, "Link /pay phải bắt đầu bằng https://app.midtrans.com/")
                return
        elif not re.match(r"^https://(?:pay\.openai\.com|app\.midtrans\.com)/", checkout_url, flags=re.IGNORECASE):
            send_message(token, chat_id, "Link /co phải bắt đầu bằng https://pay.openai.com/ hoặc https://app.midtrans.com/")
            return
        threading.Thread(
            target=_run_co_job,
            args=(token, chat_id, user_id, username, checkout_url, is_admin),
            daemon=True,
        ).start()
        return

    if command == "/covn":
        raw_args = args.strip()
        mode = raw_args.lower()
        if mode == "session":
            threading.Thread(
                target=_run_covn_job,
                args=(token, chat_id, user_id, username, is_admin),
                daemon=True,
            ).start()
            return
        if not raw_args and not document:
            _start_covn_capture(user_id, chat_id)
            send_message(token, chat_id, "Đã bật chế độ dán session cho /covn. Gửi nhiều đoạn text liên tiếp, xong thì gửi /covndone. Muốn hủy thì /covncancel.")
            return
        if document:
            try:
                payload = _download_telegram_text_document(token, document).strip()
            except Exception as e:
                send_message(token, chat_id, f"COVN_FAIL\nKhông đọc được file session: {e}")
                return
            if not payload:
                send_message(token, chat_id, "COVN_FAIL\nFile session rỗng")
                return
            _cancel_covn_capture(user_id, chat_id)
            threading.Thread(
                target=_run_covn_raw_session_job,
                args=(token, chat_id, user_id, is_admin, payload),
                daemon=True,
            ).start()
            return
        if raw_args.startswith("{"):
            if _looks_like_complete_session_json(raw_args):
                threading.Thread(
                    target=_run_covn_raw_session_job,
                    args=(token, chat_id, user_id, is_admin, raw_args),
                    daemon=True,
                ).start()
                return
            _start_covn_capture(user_id, chat_id)
            _append_covn_chunk(user_id, chat_id, raw_args)
            return
        send_message(token, chat_id, "Định dạng:\n/covn session\n/covn rồi dán nhiều đoạn text, kết thúc bằng /covndone\nhoặc\n/covn {full-json-session}\nhoặc gửi file .txt/.json với caption /covn")
        return

    if command == "/get":
        raw_args = args.strip()
        mode = raw_args.lower()
        if mode == "session":
            threading.Thread(
                target=_run_get_session_job,
                args=(token, chat_id, user_id, username, is_admin),
                daemon=True,
            ).start()
            return
        if not raw_args and not document:
            _start_get_session_capture(user_id, chat_id)
            send_message(token, chat_id, "Đã bật chế độ dán session cho /get. Gửi nhiều đoạn text liên tiếp, xong thì gửi /getdone. Muốn hủy thì /getcancel.")
            return
        if document:
            try:
                payload = _download_telegram_text_document(token, document).strip()
            except Exception as e:
                send_message(token, chat_id, f"GET_FAIL\nKhông đọc được file session: {e}")
                return
            if not payload:
                send_message(token, chat_id, "GET_FAIL\nFile session rỗng")
                return
            _cancel_get_session_capture(user_id, chat_id)
            threading.Thread(
                target=_run_get_raw_session_job,
                args=(token, chat_id, user_id, is_admin, payload),
                daemon=True,
            ).start()
            return
        if raw_args.startswith("{"):
            if _looks_like_complete_session_json(raw_args):
                threading.Thread(
                    target=_run_get_raw_session_job,
                    args=(token, chat_id, user_id, is_admin, raw_args),
                    daemon=True,
                ).start()
                return
            _start_get_session_capture(user_id, chat_id)
            _append_get_session_chunk(user_id, chat_id, raw_args)
            return
        send_message(token, chat_id, "Định dạng:\n/get session\n/get rồi dán nhiều đoạn text, kết thúc bằng /getdone\nhoặc\n/get {full-json-session}\nhoặc gửi file .txt/.json với caption /get")
        return

    if command == "/getstripe":
        raw_args = args.strip()
        mode = raw_args.lower()
        if mode == "session":
            threading.Thread(
                target=_run_getstripe_session_job,
                args=(token, chat_id, user_id, is_admin),
                daemon=True,
            ).start()
            return
        if not raw_args and not document:
            _start_getstripe_capture(user_id, chat_id)
            send_message(token, chat_id, "Đã bật chế độ dán session cho /getstripe. Gửi nhiều đoạn text liên tiếp, xong thì gửi /getstripedone. Muốn hủy thì /getstripecancel.")
            return
        if document:
            try:
                payload = _download_telegram_text_document(token, document).strip()
            except Exception as e:
                send_message(token, chat_id, f"GETSTRIPE_FAIL\nKhông đọc được file session: {e}")
                return
            if not payload:
                send_message(token, chat_id, "GETSTRIPE_FAIL\nFile session rỗng")
                return
            _cancel_getstripe_capture(user_id, chat_id)
            threading.Thread(
                target=_run_getstripe_raw_session_job,
                args=(token, chat_id, user_id, is_admin, payload),
                daemon=True,
            ).start()
            return
        if raw_args.startswith("{"):
            if _looks_like_complete_session_json(raw_args):
                threading.Thread(
                    target=_run_getstripe_raw_session_job,
                    args=(token, chat_id, user_id, is_admin, raw_args),
                    daemon=True,
                ).start()
                return
            _start_getstripe_capture(user_id, chat_id)
            _append_getstripe_chunk(user_id, chat_id, raw_args)
            return
        send_message(token, chat_id, "Định dạng:\n/getstripe session\n/getstripe rồi dán nhiều đoạn text, kết thúc bằng /getstripedone\nhoặc\n/getstripe {full-json-session}\nhoặc gửi file .txt/.json với caption /getstripe")
        return

    if command in {"/regtmail4", "/regtmail2"}:
        if not _ensure_credit_available(token, chat_id, user_id, is_admin):
            return
        quantity = 4 if command == "/regtmail4" else 2
        threading.Thread(
            target=_run_regtmail_job,
            args=(token, chat_id, user_id, username, is_admin, quantity),
            daemon=True,
        ).start()
        return

    if command == "/regtmail":
        if not _ensure_credit_available(token, chat_id, user_id, is_admin):
            return
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
    try:
        bootstrap_resp = requests.get(
            _api_url(token, "getUpdates"),
            params={"timeout": 0, "limit": 100},
            timeout=20,
        )
        bootstrap_resp.raise_for_status()
        bootstrap_data = bootstrap_resp.json()
        if bootstrap_data.get("ok") and bootstrap_data.get("result"):
            last_update_id = int(bootstrap_data["result"][-1]["update_id"])
            offset = last_update_id + 1
            _log(f"Bỏ qua {len(bootstrap_data['result'])} update backlog cũ, bắt đầu từ offset {offset}")
    except Exception as e:
        _log(f"Không sync được offset backlog lúc khởi động: {e}")

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
