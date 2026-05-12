"""
Module dịch vụ email.
Dùng tài khoản Hotmail/Outlook có OAuth2 refresh token và API dongvanfb để lấy mã xác minh.
"""

from __future__ import annotations

import html
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import urllib3

from config import (
    EMAIL_ACCOUNTS_FILE,
    EMAIL_API_TYPE,
    EMAIL_API_URL,
    EMAIL_FALLBACK_AFTER,
    EMAIL_FALLBACK_ENABLED,
    EMAIL_INITIAL_DELAY,
    EMAIL_MESSAGES_API_URL,
    EMAIL_POLL_INTERVAL,
    EMAIL_REQUEST_TIMEOUT,
    EMAIL_WAIT_TIMEOUT,
)
from utils import extract_verification_code, http_session


MESSAGE_TIME_GRACE_SECONDS = 5
REQUEST_TIMEOUT = EMAIL_REQUEST_TIMEOUT
OTP_FAST_WORKERS = 2
FAILED_ACCOUNTS_FILE = "failed_accounts.txt"
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@dataclass(frozen=True)
class HotmailAccount:
    email: str
    password: str
    refresh_token: str
    client_id: str


class EmailService:
    """
    Quản lý danh sách Hotmail và lấy mã xác minh qua API dongvanfb.

    File account có định dạng mỗi dòng:
    email@hotmail.com|password|refresh_token|client_id
    """

    def __init__(
        self,
        accounts_file: str = EMAIL_ACCOUNTS_FILE,
        api_url: str = EMAIL_API_URL,
        messages_api_url: str = EMAIL_MESSAGES_API_URL,
        api_type: str = EMAIL_API_TYPE,
        wait_timeout: int = EMAIL_WAIT_TIMEOUT,
        poll_interval: int = EMAIL_POLL_INTERVAL,
        initial_delay: int = EMAIL_INITIAL_DELAY,
        fallback_enabled: bool = EMAIL_FALLBACK_ENABLED,
        fallback_after: int = EMAIL_FALLBACK_AFTER,
        request_timeout: int = EMAIL_REQUEST_TIMEOUT,
    ):
        self.accounts_file = Path(accounts_file)
        if not self.accounts_file.is_absolute():
            self.accounts_file = Path(__file__).parent / self.accounts_file

        self.api_url = api_url
        self.messages_api_url = messages_api_url
        self.api_type = api_type
        self.wait_timeout = wait_timeout
        self.poll_interval = poll_interval
        self.initial_delay = initial_delay
        self.fallback_enabled = fallback_enabled
        self.fallback_after = fallback_after
        self.request_timeout = request_timeout
        self._accounts: list[HotmailAccount] = []
        self._next_index = 0
        self._failed_accounts: dict[str, tuple[HotmailAccount, str]] = {}
        self._retry_queue: list[HotmailAccount] = []
        self._retry_attempted_emails: set[str] = set()
        self._retry_started = False
        self._lock = threading.Lock()

    def load_accounts(self) -> list[HotmailAccount]:
        """Đọc và validate danh sách Hotmail từ file."""
        if not self.accounts_file.exists():
            print(f"❌ Không tìm thấy file tài khoản Hotmail: {self.accounts_file}")
            return []

        accounts: list[HotmailAccount] = []
        with open(self.accounts_file, "r", encoding="utf-8") as f:
            for line_number, raw_line in enumerate(f, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                parts = line.split("|")
                if len(parts) != 4:
                    print(
                        f"⚠️ Bỏ qua dòng {line_number} trong {self.accounts_file.name}: "
                        "định dạng phải là email|password|refresh_token|client_id"
                    )
                    continue

                email, password, refresh_token, client_id = (part.strip() for part in parts)
                if not email or not password or not refresh_token or not client_id:
                    print(f"⚠️ Bỏ qua dòng {line_number}: thiếu dữ liệu tài khoản Hotmail")
                    continue

                if not self._is_valid_client_id(client_id):
                    print(f"⚠️ Bỏ qua dòng {line_number}: client_id không hợp lệ")
                    continue

                accounts.append(
                    HotmailAccount(
                        email=email,
                        password=password,
                        refresh_token=refresh_token,
                        client_id=client_id,
                    )
                )

        self._accounts = accounts
        return accounts

    def _is_valid_client_id(self, client_id: str) -> bool:
        """client_id OAuth2 của Microsoft thường là UUID."""
        try:
            uuid.UUID(client_id)
            return True
        except (ValueError, TypeError):
            return False

    def _account_bundle(self, account: HotmailAccount) -> str:
        return "|".join((account.email, account.password, account.refresh_token, account.client_id))

    def _save_failed_accounts_locked(self):
        failed_file = Path(__file__).parent / FAILED_ACCOUNTS_FILE
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        with open(failed_file, "w", encoding="utf-8") as f:
            for account, reason in self._failed_accounts.values():
                f.write(f"{now}----{self._account_bundle(account)}----{reason}\n")

    def mark_account_result(self, account: HotmailAccount, success: bool, reason: str = ""):
        if not account:
            return

        with self._lock:
            key = account.email.strip().lower()
            if success:
                if key in self._failed_accounts:
                    self._failed_accounts.pop(key, None)
                    self._save_failed_accounts_locked()
                    print(f"✅ Mail retry thành công, đã gỡ khỏi {FAILED_ACCOUNTS_FILE}: {account.email}")
                return

            reason = reason or "Đăng ký thất bại"
            self._failed_accounts[key] = (account, reason)
            self._save_failed_accounts_locked()
            print(f"📝 Đã ghi mail thất bại để retry sau: {account.email} ({reason})")

    def reset_runtime_state(self, clear_failures: bool = False):
        with self._lock:
            self._next_index = 0
            self._retry_queue = []
            self._retry_attempted_emails = set()
            self._retry_started = False
            if clear_failures:
                self._failed_accounts = {}
                self._save_failed_accounts_locked()

    def get_next_account(self) -> Optional[HotmailAccount]:
        """Lấy hết danh sách chính trước, sau đó retry toàn bộ mail đã fail."""
        with self._lock:
            if not self._accounts:
                self.load_accounts()

            if not self._accounts:
                print("❌ Không có tài khoản Hotmail hợp lệ để sử dụng")
                return None

            if self._next_index < len(self._accounts):
                account = self._accounts[self._next_index]
                self._next_index += 1
                print(f"📧 Đang sử dụng Hotmail: {account.email}")
                return account

            queued_emails = {account.email.strip().lower() for account in self._retry_queue}
            retry_accounts = [
                account
                for key, (account, _) in self._failed_accounts.items()
                if key not in self._retry_attempted_emails and key not in queued_emails
            ]
            if retry_accounts:
                self._retry_queue.extend(retry_accounts)
                self._retry_started = True
                print(f"🔁 Đã hết danh sách mail chính, thêm {len(retry_accounts)} mail thất bại vào hàng retry")

            if self._retry_queue:
                account = self._retry_queue.pop(0)
                self._retry_attempted_emails.add(account.email.strip().lower())
                print(f"📧 Retry Hotmail thất bại: {account.email}")
                return account

            print("⚠️ Đã hết danh sách mail chính và không còn mail fail để retry")
            return None

    def wait_for_verification_code(
        self,
        account: HotmailAccount,
        timeout: Optional[int] = None,
        since_ts: Optional[float] = None,
        exclude_codes: Optional[set[str]] = None,
        baseline_message_ids: Optional[set[str]] = None,
    ) -> Optional[str]:
        """Gọi API định kỳ cho tới khi lấy được mã xác minh hoặc hết timeout."""
        timeout = timeout or self.wait_timeout
        since_ts = since_ts or time.time()
        print(f"⏳ Đang chờ mã xác minh cho {account.email}, tối đa {timeout} giây...")
        if self.fallback_enabled and self.fallback_after <= 0:
            print(
                "⚡ Chế độ OTP nhanh: gọi get_code_oauth2 ngay; "
                f"vẫn lọc OTP trước {datetime.fromtimestamp(since_ts).strftime('%H:%M:%S')} nếu API có timestamp"
            )
        else:
            print(
                "🔒 Chế độ chống OTP cũ: ưu tiên get_messages_oauth2 "
                f"sau {datetime.fromtimestamp(since_ts).strftime('%H:%M:%S')}; "
                f"fallback get_code_oauth2={'bật' if self.fallback_enabled else 'tắt'}"
            )
        if self.initial_delay > 0:
            print(f"⏳ Chờ {self.initial_delay} giây rồi mới gọi API đọc OTP...")
            time.sleep(self.initial_delay)

        start_time = time.time()
        last_error = None

        while time.time() - start_time < timeout:
            try:
                elapsed = time.time() - start_time
                code = self.fetch_verification_code(
                    account,
                    since_ts=since_ts,
                    exclude_codes=exclude_codes,
                    baseline_message_ids=baseline_message_ids,
                    allow_fallback=self.fallback_enabled and elapsed >= self.fallback_after,
                )
                if code:
                    print(f"\n✅ Đã lấy được mã xác minh: {code}")
                    return code
            except Exception as e:
                last_error = e
                print(f"  ⚠️ Lỗi khi gọi API lấy mã: {e}")

            elapsed = int(time.time() - start_time)
            print(f"  Đang chờ mã... ({elapsed} giây)", end="\r")
            time.sleep(self.poll_interval)

        if last_error:
            print(f"\n⏰ Hết thời gian chờ mã xác minh, lỗi gần nhất: {last_error}")
        else:
            print("\n⏰ Hết thời gian chờ mã xác minh")
        return None

    def fetch_verification_code(
        self,
        account: HotmailAccount,
        since_ts: Optional[float] = None,
        exclude_codes: Optional[set[str]] = None,
        baseline_message_ids: Optional[set[str]] = None,
        allow_fallback: bool = False,
    ) -> Optional[str]:
        """Ưu tiên luồng nhanh khi fallback bật ngay, ngược lại dùng messages để lọc OTP cũ."""
        if allow_fallback and self.fallback_after <= 0:
            return self.fetch_verification_code_fast(
                account,
                since_ts=since_ts,
                exclude_codes=exclude_codes,
                baseline_message_ids=baseline_message_ids,
            )

        try:
            code = self.fetch_code_from_messages(
                account,
                since_ts=since_ts,
                exclude_codes=exclude_codes,
                baseline_message_ids=baseline_message_ids,
            )
            if code:
                return code
        except Exception as e:
            print(f"  ⚠️ API get_messages_oauth2 lỗi: {e}")

        if not allow_fallback:
            return None
        print("  ℹ️ Chưa thấy mail mới qua messages, fallback get_code_oauth2")
        return self.fetch_code_primary(account, since_ts=since_ts, exclude_codes=exclude_codes)

    def fetch_verification_code_fast(
        self,
        account: HotmailAccount,
        since_ts: Optional[float] = None,
        exclude_codes: Optional[set[str]] = None,
        baseline_message_ids: Optional[set[str]] = None,
    ) -> Optional[str]:
        """Gọi song song get_code_oauth2 và get_messages_oauth2, lấy OTP hợp lệ đầu tiên."""
        tasks = []
        executor = ThreadPoolExecutor(max_workers=OTP_FAST_WORKERS)
        try:
            tasks.append((
                "get_code_oauth2",
                executor.submit(
                    self.fetch_code_primary,
                    account,
                    since_ts=since_ts,
                    exclude_codes=exclude_codes,
                ),
            ))
            tasks.append((
                "get_messages_oauth2",
                executor.submit(
                    self.fetch_code_from_messages,
                    account,
                    since_ts=since_ts,
                    exclude_codes=exclude_codes,
                    baseline_message_ids=baseline_message_ids,
                ),
            ))

            future_names = {future: name for name, future in tasks}
            for future in as_completed(future_names):
                name = future_names[future]
                try:
                    code = future.result()
                except Exception as e:
                    print(f"  ⚠️ API {name} lỗi: {e}")
                    continue
                if code:
                    for _, pending in tasks:
                        if pending is not future:
                            pending.cancel()
                    return code
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        return None

    def fetch_code_from_messages(
        self,
        account: HotmailAccount,
        since_ts: Optional[float] = None,
        exclude_codes: Optional[set[str]] = None,
        baseline_message_ids: Optional[set[str]] = None,
    ) -> Optional[str]:
        """Gọi get_messages_oauth2 và lọc OTP từ mail mới."""
        messages = self.fetch_messages(account)
        print(f"  📬 get_messages_oauth2 trả về {len(messages)} mail")
        if not messages:
            return None

        return self._extract_code_from_messages(
            messages,
            expected_email=account.email,
            since_ts=since_ts,
            exclude_codes=exclude_codes,
            baseline_message_ids=baseline_message_ids,
        )

    def fetch_messages(self, account: HotmailAccount) -> list[dict]:
        """Gọi get_messages_oauth2 và trả về danh sách mail đã chuẩn hóa."""
        payload = {
            "email": account.email,
            "refresh_token": account.refresh_token,
            "client_id": account.client_id,
            "list_mail": "all",
        }
        headers = {
            "user-agent": "Mozilla/5.0",
            "content-type": "application/json",
        }

        response = http_session.post(
            self.messages_api_url,
            json=payload,
            timeout=self.request_timeout,
            verify=False,
            headers=headers,
        )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")

        data = response.json()
        response_email = self._response_email(data)
        if response_email and response_email.lower() != account.email.lower():
            print(f"  ⚠️ API trả mailbox khác ({response_email}), bỏ qua vì đang chờ {account.email}")
            return []

        return self._extract_messages(data)

    def snapshot_message_ids(self, account: HotmailAccount) -> Optional[set[str]]:
        """Chụp UID/ID mail hiện có trước khi yêu cầu OTP."""
        try:
            messages = self.fetch_messages(account)
        except Exception as e:
            print(f"⚠️ Không chụp được baseline mail trước OTP: {e}")
            return None

        message_ids = {self._message_id(message) for message in messages}
        message_ids.discard("")
        print(f"📬 Baseline mailbox trước OTP: {len(message_ids)} mail id")
        return message_ids

    def fetch_code_primary(
        self,
        account: HotmailAccount,
        since_ts: Optional[float] = None,
        exclude_codes: Optional[set[str]] = None,
    ) -> Optional[str]:
        """Gọi API get_code_oauth2 một lần và trả về mã nếu có."""
        payload = {
            "email": account.email,
            "refresh_token": account.refresh_token,
            "client_id": account.client_id,
            "type": self.api_type,
        }
        headers = {
            "user-agent": "Mozilla/5.0",
            "content-type": "application/json",
        }

        response = http_session.post(
            self.api_url,
            json=payload,
            timeout=self.request_timeout,
            headers=headers,
        )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")

        data = response.json()
        response_email = self._response_email(data)
        if response_email and response_email.lower() != account.email.lower():
            print(f"  ⚠️ get_code_oauth2 trả mailbox khác ({response_email}), bỏ qua vì đang chờ {account.email}")
            return None
        nested_data = data.get("data")
        source = nested_data if isinstance(nested_data, dict) else data
        raw_date = source.get("date") or data.get("date") or ""
        msg_ts = self._parse_timestamp(raw_date)
        if self._is_before_since(raw_date, msg_ts, since_ts):
            print(f"  ℹ️ get_code_oauth2 trả OTP cũ date={raw_date}, bỏ qua")
            return None

        code = self._extract_explicit_code(data, source)
        if code:
            if exclude_codes and code in exclude_codes:
                return None
            print(f"📧 Tìm thấy OTP trực tiếp từ get_code_oauth2 | type={self.api_type} | date={raw_date or 'không có'}")
            return code

        text_blob = " ".join(
            str(source.get(key, "") or data.get(key, ""))
            for key in ("subject", "message", "content", "body", "from", "sender")
        )
        code = self._extract_openai_code(text_blob)
        if not code and self.api_type != "all":
            code = extract_verification_code(self._html_to_text(text_blob))

        if code and exclude_codes and code in exclude_codes:
            return None
        if code:
            print(f"📧 Tìm thấy OTP từ get_code_oauth2 | type={self.api_type} | date={raw_date or 'không có'}")
        return code

    def _extract_explicit_code(self, *containers: Any) -> Optional[str]:
        """Lấy OTP từ field rõ ràng của API, tránh nhầm status code như 200."""
        for container in containers:
            if not isinstance(container, dict):
                continue

            for key in ("otp", "verification_code", "verify_code", "mail_code", "email_code"):
                code = self._normalize_otp_value(container.get(key))
                if code:
                    return code

            code = self._normalize_otp_value(container.get("code"))
            if code and code not in {"200", "201"}:
                return code

        return None

    def _normalize_otp_value(self, value: Any) -> Optional[str]:
        if value is None:
            return None

        text = str(value).strip()
        if re.fullmatch(r"\d{6}", text):
            return text

        match = re.search(r"\b(\d{6})\b", text)
        return match.group(1) if match else None

    def _extract_messages(self, data: Any) -> list[dict]:
        """Chuẩn hóa response API messages về list dict."""
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]

        if not isinstance(data, dict):
            return []

        candidates = [
            data.get("messages"),
            data.get("mails"),
            data.get("results"),
            data.get("data"),
        ]
        for candidate in candidates:
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
            if isinstance(candidate, dict):
                for key in ("messages", "mails", "results", "list"):
                    nested = candidate.get(key)
                    if isinstance(nested, list):
                        return [item for item in nested if isinstance(item, dict)]

        return []

    def _response_email(self, data: Any) -> str:
        """Lấy email mailbox từ response nếu API có trả."""
        if not isinstance(data, dict):
            return ""

        for key in ("email", "mail", "account"):
            value = data.get(key)
            if isinstance(value, str) and "@" in value:
                return value.strip()

        nested = data.get("data")
        if isinstance(nested, dict):
            for key in ("email", "mail", "account"):
                value = nested.get(key)
                if isinstance(value, str) and "@" in value:
                    return value.strip()

        return ""

    def _extract_code_from_messages(
        self,
        messages: list[dict],
        expected_email: str,
        since_ts: Optional[float] = None,
        exclude_codes: Optional[set[str]] = None,
        baseline_message_ids: Optional[set[str]] = None,
    ) -> Optional[str]:
        """Lọc mail mới rồi trích OTP từ subject/message/body."""
        sorted_messages = sorted(
            messages,
            key=lambda item: self._message_timestamp(item) or 0,
            reverse=True,
        )

        skipped_old = 0
        skipped_no_ts = 0
        skipped_no_keyword = 0
        skipped_wrong_mailbox = 0
        skipped_baseline = 0
        for message in sorted_messages:
            message_email = self._message_email(message)
            if message_email and message_email.lower() != expected_email.lower():
                skipped_wrong_mailbox += 1
                continue

            msg_ts = self._message_timestamp(message)
            if since_ts:
                if not msg_ts:
                    message_id = self._message_id(message)
                    if not message_id:
                        skipped_no_ts += 1
                        continue
                    if baseline_message_ids is None:
                        skipped_no_ts += 1
                        continue
                    if message_id in baseline_message_ids:
                        skipped_baseline += 1
                        continue
                raw_date = (
                    message.get("date")
                    or message.get("time")
                    or message.get("timestamp")
                    or message.get("created_at")
                    or message.get("received_at")
                    or ""
                )
                if self._is_before_since(raw_date, msg_ts, since_ts):
                    skipped_old += 1
                    continue

            text_blob = " ".join(
                str(message.get(key, ""))
                for key in (
                    "subject",
                    "message",
                    "body",
                    "content",
                    "text",
                    "html",
                    "from",
                    "sender",
                )
            )

            lowered = text_blob.lower()
            if not any(keyword in lowered for keyword in (
                "openai",
                "chatgpt",
                "verification",
                "verify",
                "code",
                "mã xác minh",
                "ma xac minh",
                "tạm thời",
                "tam thoi",
            )):
                skipped_no_keyword += 1
                continue

            code = self._extract_openai_code(text_blob)
            if code:
                if exclude_codes and code in exclude_codes:
                    continue
                subject = message.get("subject", "")
                raw_date = (
                    message.get("date")
                    or message.get("time")
                    or message.get("timestamp")
                    or message.get("created_at")
                    or message.get("received_at")
                    or ""
                )
                message_id = self._message_id(message)
                print(
                    "📧 Tìm thấy OTP từ get_messages_oauth2 "
                    f"| id={message_id} | date={raw_date or 'không có'} | subject={subject}"
                )
                return code

        if skipped_old or skipped_no_ts or skipped_wrong_mailbox or skipped_no_keyword or skipped_baseline:
            print(
                "  ℹ️ Không thấy OTP mới hợp lệ "
                f"(bỏ qua mail cũ={skipped_old}, không có timestamp={skipped_no_ts}, "
                f"nằm trong baseline={skipped_baseline}, sai mailbox={skipped_wrong_mailbox}, "
                f"không đúng keyword={skipped_no_keyword})"
            )
        return None

    def _message_id(self, message: dict) -> str:
        """Lấy định danh ổn định của mail."""
        for key in ("uid", "id", "message_id", "mail_id", "mid"):
            value = message.get(key)
            if value is not None:
                return str(value).strip()

        # Fallback cuối nếu API không có uid/id nhưng có subject + nội dung.
        subject = str(message.get("subject", "")).strip()
        body = str(message.get("message") or message.get("body") or message.get("content") or "").strip()
        if subject or body:
            return f"{subject}|{body[:80]}"

        return ""

    def _message_email(self, message: dict) -> str:
        """Lấy email mailbox/recipient từ message nếu có."""
        for key in ("email", "to", "recipient", "mailbox"):
            value = message.get(key)
            if isinstance(value, str) and "@" in value:
                return value.strip()
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and "@" in item:
                        return item.strip()
                    if isinstance(item, dict):
                        nested = item.get("email") or item.get("address")
                        if isinstance(nested, str) and "@" in nested:
                            return nested.strip()
        return ""

    def _extract_openai_code(self, content: str) -> Optional[str]:
        """Chỉ extract OTP từ nội dung OpenAI/ChatGPT hợp lệ."""
        if not content:
            return None

        content = self._html_to_text(content)
        lowered = content.lower()
        if "openai" not in lowered and "chatgpt" not in lowered:
            return None

        phrase_patterns = [
            r"enter\s+this\s+temporary\s+verification\s+code\s+to\s+continue[:\s]+(\d{6})",
            r"temporary\s+verification\s+code\s+to\s+continue[:\s]+(\d{6})",
            r"your\s+(?:openai|chatgpt)?\s*verification\s+code\s+is[:\s]+(\d{6})",
            r"(\d{6})\s+is\s+your\s+(?:openai|chatgpt)?\s*verification\s+code",
            r"(?:openai|chatgpt)\s+verification\s+code[:\s]+(\d{6})",
            r"(?:openai|chatgpt).*?code\s+is[:\s]+(\d{6})",
            r"nhập\s+mã\s+xác\s+minh\s+tạm\s+thời\s+này\s+để\s+tiếp\s+tục[:\s]+(\d{6})",
            r"ma\s+xac\s+minh\s+tam\s+thoi\s+nay\s+de\s+tiep\s+tuc[:\s]+(\d{6})",
            r"mã\s+xác\s+minh\s+tạm\s+thời\s+này\s+để\s+tiếp\s+tục[:\s]+(\d{6})",
            r"ma\s+xac\s+minh\s+tam\s+thoi\s+nay\s+de\s+tiep\s+tuc[:\s]+(\d{6})",
            r"mã\s+xác\s+minh\s+tạm\s+thời.*?(\d{6})",
            r"ma\s+xac\s+minh\s+tam\s+thoi.*?(\d{6})",
        ]
        for pattern in phrase_patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                return match.group(1)

        return extract_verification_code(content)

    def _html_to_text(self, content: str) -> str:
        """Chuyển HTML email thành text để tránh bắt nhầm số trong CSS/URL."""
        text = html.unescape(str(content))
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>", " ", text)
        text = re.sub(r"(?i)</(?:p|div|td|tr|table|center|span|b|strong|h[1-6])>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _message_timestamp(self, message: dict) -> Optional[float]:
        """Đọc timestamp từ nhiều format response khác nhau."""
        for key in ("date", "time", "timestamp", "created_at", "received_at"):
            value = message.get(key)
            parsed = self._parse_timestamp(value)
            if parsed:
                return parsed
        return None

    def _parse_timestamp(self, value: Any) -> Optional[float]:
        if value is None:
            return None

        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp = timestamp / 1000
            return timestamp

        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.isdigit():
                return self._parse_timestamp(float(text))
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f",
                "%d/%m/%Y %H:%M:%S",
                "%H:%M - %d/%m/%Y",
            ):
                try:
                    return datetime.strptime(text.replace("Z", ""), fmt).timestamp()
                except ValueError:
                    continue
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None

        return None

    def _is_before_since(self, raw_date: Any, msg_ts: Optional[float], since_ts: Optional[float]) -> bool:
        if not since_ts or not msg_ts:
            return False

        grace = MESSAGE_TIME_GRACE_SECONDS
        if isinstance(raw_date, str) and re.match(r"^\s*\d{1,2}:\d{2}\s+-\s+\d{1,2}/\d{1,2}/\d{4}\s*$", raw_date):
            grace = 65

        return msg_ts + grace < float(since_ts)


email_service = EmailService()


def create_temp_email():
    """
    Hàm tương thích với luồng cũ.
    Trả về email Hotmail và object account thay cho JWT token.
    """
    account = email_service.get_next_account()
    if not account:
        return None, None
    return account.email, account


def mark_account_result(account: HotmailAccount, success: bool, reason: str = ""):
    """Ghi nhận kết quả để retry mail fail sau khi hết danh sách chính."""
    return email_service.mark_account_result(account, success, reason=reason)


def reset_runtime_state(clear_failures: bool = False):
    """Reset con trỏ mail khi bắt đầu tác vụ mới trong cùng process server."""
    return email_service.reset_runtime_state(clear_failures=clear_failures)


def wait_for_verification_email(
    account: HotmailAccount,
    timeout: int = None,
    since_ts: float = None,
    exclude_codes: set[str] = None,
    baseline_message_ids: set[str] = None,
):
    """Hàm tương thích với luồng cũ, nhận HotmailAccount thay cho JWT token."""
    if not account:
        return None
    return email_service.wait_for_verification_code(
        account,
        timeout=timeout,
        since_ts=since_ts,
        exclude_codes=exclude_codes,
        baseline_message_ids=baseline_message_ids,
    )


def snapshot_message_ids(account: HotmailAccount):
    """Chụp danh sách UID/ID mail hiện tại cho luồng đăng ký."""
    if not account:
        return None
    return email_service.snapshot_message_ids(account)
