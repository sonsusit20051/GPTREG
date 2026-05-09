from __future__ import annotations

import html as html_lib
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import requests


TMAIL_PROVIDER = "tempmailmmo"
TEMPMAILMMO_BASE_URL = "https://tempmailmmo.com/tool"
TMAIL_DOMAIN_MODE = "all"
TMAIL_FOCUS_DOMAINS = (
    "liscensekey.io.vn",
    "phucuongth.edu.vn",
)
TMAIL_BLOCKED_DOMAINS = ()
DEFAULT_REQUEST_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"


@dataclass
class TempMailAccount:
    email: str
    password: str = ""
    refresh_token: str = ""
    client_id: str = ""
    provider: str = "tmail"
    api: Any = field(default=None, repr=False, compare=False)
    domain: str = ""
    user: str = ""


def _normalize_domain(domain: str) -> str:
    return str(domain or "").strip().lstrip("@").lower()


def get_domain_mode() -> str:
    return str(TMAIL_DOMAIN_MODE or "all").strip().lower()


def get_focus_domains() -> list[str]:
    return [_normalize_domain(item) for item in TMAIL_FOCUS_DOMAINS if _normalize_domain(item)]


def get_blocked_domains() -> list[str]:
    return [_normalize_domain(item) for item in TMAIL_BLOCKED_DOMAINS if _normalize_domain(item)]


def set_domain_mode_all() -> None:
    global TMAIL_DOMAIN_MODE
    TMAIL_DOMAIN_MODE = "all"


def set_focus_domains(domains: list[str] | tuple[str, ...]) -> list[str]:
    global TMAIL_DOMAIN_MODE, TMAIL_FOCUS_DOMAINS
    normalized: list[str] = []
    for item in domains:
        domain = _normalize_domain(item)
        if domain and domain not in normalized:
            normalized.append(domain)
    if not normalized:
        raise ValueError("Thiếu domain hợp lệ.")
    TMAIL_FOCUS_DOMAINS = tuple(normalized)
    TMAIL_DOMAIN_MODE = "focus"
    return normalized


def add_blocked_domains(domains: list[str] | tuple[str, ...]) -> list[str]:
    global TMAIL_BLOCKED_DOMAINS
    current = get_blocked_domains()
    for item in domains:
        domain = _normalize_domain(item)
        if domain and domain not in current:
            current.append(domain)
    if not current:
        raise ValueError("Thiếu domain hợp lệ.")
    TMAIL_BLOCKED_DOMAINS = tuple(current)
    return current


def remove_blocked_domains(domains: list[str] | tuple[str, ...]) -> list[str]:
    global TMAIL_BLOCKED_DOMAINS
    to_remove = {_normalize_domain(item) for item in domains if _normalize_domain(item)}
    current = [domain for domain in get_blocked_domains() if domain not in to_remove]
    TMAIL_BLOCKED_DOMAINS = tuple(current)
    return current


def clear_blocked_domains() -> None:
    global TMAIL_BLOCKED_DOMAINS
    TMAIL_BLOCKED_DOMAINS = ()


def describe_domain_mode() -> str:
    mode = get_domain_mode()
    focus_domains = get_focus_domains()
    blocked_domains = get_blocked_domains()
    base = "focus: " + ", ".join(focus_domains) if mode == "focus" else "all"
    if blocked_domains:
        base += " | blocked: " + ", ".join(blocked_domains)
    return base


class TempMailMMOAPI:
    def __init__(self, base_url: str = TEMPMAILMMO_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.sid_token = None
        self.email_address = None
        self.email_user = None
        self.email_domain = None
        self.mailbox_ready_at = None
        self.otp_baseline_at = None
        self.consumed_otp_mail_ids: set[str] = set()

    def _request_post(self, action: str, data: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
        result: dict[str, Any] | list[Any] | None = None
        response = None
        for attempt in range(2):
            response = self.session.post(
                f"{self.base_url}/ajax.php?f={action}",
                data=data,
                timeout=timeout,
            )
            try:
                result = response.json()
            except Exception:
                result = {"error": (response.text or "").strip()[:200] or f"HTTP_{response.status_code}"}
            if response.status_code != 429 or attempt == 1:
                break
            retry_after = response.headers.get("Retry-After")
            try:
                sleep_time = max(1, min(10, int(float(retry_after))))
            except Exception:
                sleep_time = 3
            time.sleep(sleep_time)
        if response is not None and response.status_code >= 400:
            if not isinstance(result, dict):
                result = {"error": response.text[:200] or f"HTTP_{response.status_code}"}
            result.setdefault("status_code", response.status_code)
        if isinstance(result, dict):
            self._update_session_from_response(result)
            return result
        return {"data": result or []}

    def _update_session_from_response(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        data = self._unwrap_payload(data)
        self.sid_token = data.get("sid_token") or data.get("sid") or data.get("token") or self.sid_token
        self.email_address = data.get("email_addr") or data.get("email") or data.get("address") or self.email_address
        self.email_user = data.get("email_user") or data.get("user") or self.email_user
        self.email_domain = data.get("email_domain") or data.get("domain") or self.email_domain

    def get_domains(self) -> list[str]:
        try:
            response = self.session.get(f"{self.base_url}/ajax.php?f=get_domains", timeout=10)
            response.raise_for_status()
            data = response.json()
            domains = data.get("domains", [])
            return [_normalize_domain(item) for item in domains if _normalize_domain(item)]
        except Exception as e:
            print(f"   ⚠️ Lỗi get_domains: {e}")
            return []

    def create_email(self, email_domain: str | None = None, lang: str = "vi") -> Optional[dict[str, Any]]:
        try:
            payload: dict[str, Any] = {"lang": lang}
            if email_domain:
                payload["email_domain"] = _normalize_domain(email_domain)
            result = self._request_post("get_email_address", payload, timeout=15)
            if "error" in result:
                print(f"   ⚠️ Lỗi tạo email: {result['error']}")
                return None
            self.mailbox_ready_at = time.time()
            self.otp_baseline_at = self.mailbox_ready_at
            return {
                "email": self.email_address,
                "sid_token": self.sid_token,
                "email_user": self.email_user,
                "email_domain": self.email_domain,
            }
        except Exception as e:
            print(f"   ⚠️ Lỗi create_email: {e}")
            return None

    def mark_otp_baseline(self, seconds_back: int = 15) -> None:
        self.otp_baseline_at = time.time() - max(0, int(seconds_back or 0))

    def get_email_list(self, offset: int = 0) -> list[dict[str, Any]]:
        if not self.sid_token:
            print("   ⚠️ Chưa có sid_token, cần gọi create_email trước")
            return []
        try:
            result = self._request_post(
                "get_email_list",
                {"sid_token": self.sid_token, "offset": offset},
                timeout=10,
            )
            if "error" in result:
                print(f"   ⚠️ Lỗi get_email_list: {result['error']}")
                return []
            if isinstance(result, list):
                return result
            payload = self._unwrap_payload(result)
            for key in ("list", "emails", "data", "messages"):
                value = payload.get(key) if isinstance(payload, dict) else None
                if isinstance(value, list):
                    return value
            return []
        except Exception as e:
            print(f"   ⚠️ Lỗi get_email_list: {e}")
            return []

    def fetch_email(self, email_id: Any) -> Optional[dict[str, Any]]:
        if not self.sid_token:
            print("   ⚠️ Chưa có sid_token, cần gọi create_email trước")
            return None
        try:
            result = self._request_post(
                "fetch_email",
                {"sid_token": self.sid_token, "email_id": email_id},
                timeout=10,
            )
            if "error" in result:
                print(f"   ⚠️ Lỗi fetch_email: {result['error']}")
                return None
            return result
        except Exception as e:
            print(f"   ⚠️ Lỗi fetch_email: {e}")
            return None

    def _strip_html(self, value: str) -> str:
        text = str(value or "")
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
        text = re.sub(r"(?is)<br\s*/?>", "\n", text)
        text = re.sub(r"(?is)</p\s*>|</div\s*>|</tr\s*>", "\n", text)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = html_lib.unescape(text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _collect_text_fragments(self, value: Any, limit: int = 200) -> list[str]:
        fragments: list[str] = []

        def walk(node: Any) -> None:
            if len(fragments) >= limit:
                return
            if node is None:
                return
            if isinstance(node, str):
                cleaned = self._strip_html(node)
                if cleaned:
                    fragments.append(cleaned)
                return
            if isinstance(node, (int, float)):
                fragments.append(str(node))
                return
            if isinstance(node, dict):
                for key, child in node.items():
                    if len(fragments) >= limit:
                        break
                    if isinstance(key, str):
                        key_lower = key.lower()
                        if key_lower in {"sid_token", "token", "cookie", "headers", "header"}:
                            continue
                    walk(child)
                return
            if isinstance(node, (list, tuple, set)):
                for child in node:
                    if len(fragments) >= limit:
                        break
                    walk(child)

        walk(value)
        deduped: list[str] = []
        seen = set()
        for item in fragments:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped

    @staticmethod
    def _unwrap_payload(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        for key in ("email", "mail", "message", "data", "detail", "item"):
            nested = value.get(key)
            if isinstance(nested, dict):
                merged = dict(nested)
                for outer_key, outer_value in value.items():
                    if outer_key not in ("email", "mail", "message", "data", "detail", "item"):
                        merged.setdefault(outer_key, outer_value)
                return merged
        return value

    @staticmethod
    def _first_value(mapping: dict[str, Any], keys: tuple[str, ...], default: str = "") -> str:
        if not isinstance(mapping, dict):
            return default
        for key in keys:
            value = mapping.get(key)
            if value not in (None, ""):
                return str(value)
        return default

    @staticmethod
    def _normalize_otp_code(value: str) -> str:
        code = re.sub(r"\D", "", str(value or ""))
        return code if len(code) == 6 and code.isdigit() else ""

    def _mail_timestamp_score(self, email_item: dict[str, Any]) -> float:
        email_item = self._unwrap_payload(email_item)
        raw = (
            email_item.get("mail_timestamp")
            or email_item.get("timestamp")
            or email_item.get("created_at")
            or email_item.get("created")
            or email_item.get("date")
            or email_item.get("mail_date")
            or email_item.get("datetime")
            or email_item.get("received_at")
            or email_item.get("time")
            or email_item.get("mail_time")
            or 0
        )
        try:
            value = float(raw)
            if value > 10_000_000_000:
                value /= 1000.0
            return value
        except Exception:
            pass
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return 0

    def _mail_is_too_old(self, email_item: dict[str, Any], not_before: float | None = None) -> bool:
        if not_before is None:
            not_before = self.otp_baseline_at
        if not not_before:
            return False
        timestamp = self._mail_timestamp_score(email_item)
        if not timestamp:
            return False
        return timestamp < (float(not_before) - 30)

    def _is_openai_related(self, email_item: dict[str, Any], detail: Optional[dict[str, Any]] = None, allow_generic: bool = False) -> bool:
        email_item = self._unwrap_payload(email_item)
        detail = self._unwrap_payload(detail or {})
        sender = " ".join([
            self._first_value(email_item, ("mail_from", "from", "sender", "from_name")),
            self._first_value(detail, ("mail_from", "from", "sender", "from_name")),
        ]).lower()
        subject = " ".join([
            self._first_value(email_item, ("mail_subject", "subject", "title")),
            self._first_value(detail, ("mail_subject", "subject", "title")),
        ]).lower()
        excerpt = " ".join([
            self._first_value(email_item, ("mail_excerpt", "excerpt", "bodyPreview", "preview")),
            self._first_value(detail, ("mail_excerpt", "excerpt", "bodyPreview", "preview")),
        ]).lower()
        raw_body = self._first_value(
            detail,
            ("mail_body", "body", "html", "text", "mail_html", "html_body", "content", "message"),
        ) or self._first_value(email_item, ("mail_excerpt", "excerpt", "bodyPreview", "preview"))
        body = self._strip_html(raw_body).lower()
        strong_keywords = ("openai", "chatgpt", "tm.openai.com")
        if any(keyword in sender for keyword in strong_keywords):
            return True
        if any(keyword in subject for keyword in strong_keywords):
            return True
        if any(keyword in excerpt for keyword in strong_keywords):
            return True
        if any(keyword in body[:5000] for keyword in strong_keywords):
            return True
        if allow_generic:
            generic_keywords = ("verification code", "security code", "one-time code", "passcode")
            return any(keyword in subject or keyword in excerpt or keyword in body[:1500] for keyword in generic_keywords)
        return False

    def _score_otp_candidate(self, code: str, text: str, start: int, end: int, subject: str = "", sender: str = "") -> int:
        if not code or not code.isdigit() or len(code) != 6:
            return -999
        lower_text = str(text or "").lower()
        context = lower_text[max(0, start - 90):min(len(lower_text), end + 90)]
        before = lower_text[max(0, start - 45):start]
        after = lower_text[end:min(len(lower_text), end + 45)]
        haystack = f"{subject} {sender}".lower()
        score = 0
        if len(set(code)) == 1:
            score -= 40
        if code[:3] in ("199", "200", "201", "202", "203"):
            score -= 35
        if "openai" in haystack or "chatgpt" in haystack:
            score += 12
        if "verification" in haystack or "verify" in haystack:
            score += 6
        strong_phrases = (
            "verification code", "temporary verification code", "your code", "code is",
            "login code", "security code", "one-time code", "authentication code",
            "confirmation code", "passcode", "enter this", "otp",
        )
        for phrase in strong_phrases:
            if phrase in context:
                score += 55
                break
        if any(word in before for word in ("code", "verification", "verify", "temporary", "enter", "otp", "security", "passcode")):
            score += 28
        if any(word in after for word in ("code", "verification", "verify", "temporary", "otp", "security", "passcode")):
            score += 16
        bad_context = (
            "copyright", "privacy", "terms", "unsubscribe", "address", "street", "postal",
            "zipcode", "zip code", "phone", "invoice", "order", "reference", "tracking",
            "width", "height", "padding", "margin", "font-size", "border", "utm_",
            "http", "2024", "2025", "2026",
        )
        if any(word in context for word in bad_context):
            score -= 45
        return score

    def extract_best_otp(self, detail: dict[str, Any], list_item: Optional[dict[str, Any]] = None) -> tuple[str, dict[str, Any]]:
        detail = self._unwrap_payload(detail or {})
        list_item = self._unwrap_payload(list_item or {})
        subject = self._first_value(detail, ("mail_subject", "subject", "title")) or self._first_value(list_item, ("mail_subject", "subject", "title"))
        sender = self._first_value(detail, ("mail_from", "from", "sender", "from_name")) or self._first_value(list_item, ("mail_from", "from", "sender", "from_name"))
        raw_body = self._first_value(
            detail,
            ("mail_body", "body", "html", "text", "mail_html", "html_body", "content", "message"),
        ) or self._first_value(list_item, ("mail_excerpt", "excerpt", "bodyPreview", "preview"))
        sources = [
            ("subject", self._strip_html(subject)),
            ("body", self._strip_html(raw_body)),
        ]
        extra_text = " ".join(self._collect_text_fragments({"detail": detail, "list_item": list_item}, limit=250))
        if extra_text:
            sources.append(("payload", extra_text))
        if not any(text for _, text in sources):
            return "", {}

        candidates: list[dict[str, Any]] = []
        seen = set()
        hint_patterns = [
            re.compile(r"(?:verification|verify|security|login|temporary|one[- ]?time|authentication|confirmation|passcode|code|otp)[^0-9]{0,90}(?<!\d)(\d(?:[\s\-]*\d){5})(?!\d)", re.I | re.S),
            re.compile(r"(?<!\d)(\d(?:[\s\-]*\d){5})(?!\d)[^a-zA-Z0-9]{0,60}(?:is|for|to|as|:)?[^a-zA-Z0-9]{0,30}(?:your|the|this)?[^a-zA-Z0-9]{0,30}(?:verification|verify|security|login|temporary|one[- ]?time|authentication|confirmation|passcode|code|otp)", re.I | re.S),
            re.compile(r"(?:mã|xac minh|xác minh|ma)[^0-9]{0,90}(?<!\d)(\d(?:[\s\-]*\d){5})(?!\d)", re.I | re.S),
        ]

        def add_candidate(source_name: str, text: str, match: re.Match[str], pattern_bonus: int = 0, pattern_index: int | None = None) -> None:
            raw_code = match.group(1)
            code = self._normalize_otp_code(raw_code)
            if not code:
                return
            start = match.start(1)
            end = match.end(1)
            key = (source_name, code, start, end)
            if key in seen:
                return
            seen.add(key)
            score = self._score_otp_candidate(code, text, start, end, subject=subject, sender=sender) + pattern_bonus
            if source_name == "subject":
                score += 20
            context = text[max(0, start - 90):min(len(text), end + 90)]
            candidates.append({
                "code": code,
                "score": score,
                "context": " ".join(context.split()),
                "subject": subject,
                "sender": sender,
                "source": source_name,
                "pattern_index": pattern_index,
            })

        for source_name, text in sources:
            if not text:
                continue
            for pattern_index, pattern in enumerate(hint_patterns):
                for match in pattern.finditer(text):
                    add_candidate(source_name, text, match, pattern_bonus=70 - (pattern_index * 10), pattern_index=pattern_index)
            for match in re.finditer(r"(?<!\d)(\d{6})(?!\d)", text):
                add_candidate(source_name, text, match)

        if not candidates:
            return "", {}
        candidates.sort(key=lambda item: item["score"], reverse=True)
        best = candidates[0]
        if best["score"] < 25:
            return "", {"candidates": candidates[:5], "reason": "low_score"}
        return best["code"], best

    def wait_for_otp(
        self,
        max_wait_time: int = 180,
        poll_interval: int = 5,
        subject_contains: str | None = None,
        sender_contains: str | None = None,
        require_openai: bool = False,
        not_before: float | None = None,
    ) -> tuple[Optional[str], Optional[dict[str, Any]]]:
        start_time = time.time()
        last_count = 0
        poll_num = 0
        poll_interval = max(2, min(poll_interval, 30))
        seen_mail_ids: set[str] = set()

        while time.time() - start_time < max_wait_time:
            try:
                emails = self.get_email_list()
            except Exception as e:
                print(f"   ⚠️ get_email_list lỗi: {e}")
                time.sleep(poll_interval)
                continue

            poll_num += 1
            if not emails:
                if poll_num <= 3 or poll_num % 6 == 0:
                    print(f"   ⏳ Chờ thư (lần {poll_num})... chưa có email")
                time.sleep(poll_interval)
                continue

            current_count = len(emails)
            if poll_num <= 2 or current_count != last_count:
                print(f"   📧 Có {current_count} email trong hộp thư")
            last_count = current_count

            sorted_emails = sorted(emails, key=self._mail_timestamp_score, reverse=True)
            latest_email: Optional[dict[str, Any]] = None
            for email in sorted_emails:
                email = self._unwrap_payload(email)
                if self._mail_is_too_old(email, not_before=not_before):
                    continue
                mail_from = email.get("mail_from", "") or email.get("from", "")
                mail_subject = email.get("mail_subject", "") or email.get("subject", "")
                if sender_contains and sender_contains.lower() not in str(mail_from).lower():
                    continue
                if subject_contains and subject_contains.lower() not in str(mail_subject).lower():
                    continue
                latest_email = email
                break

            if not latest_email:
                time.sleep(poll_interval)
                continue

            mail_id = latest_email.get("mail_id") or latest_email.get("id") or latest_email.get("email_id")
            if not mail_id:
                time.sleep(poll_interval)
                continue

            mail_id_str = str(mail_id)
            if mail_id_str in self.consumed_otp_mail_ids:
                time.sleep(poll_interval)
                continue
            if mail_id_str in seen_mail_ids and poll_num % 3 != 0:
                time.sleep(poll_interval)
                continue

            email_detail = self.fetch_email(mail_id)
            if not email_detail:
                time.sleep(poll_interval)
                continue
            email_detail = self._unwrap_payload(email_detail)
            seen_mail_ids.add(mail_id_str)

            otp, meta = self.extract_best_otp(email_detail, list_item=latest_email)
            if otp:
                self.consumed_otp_mail_ids.add(mail_id_str)
                subject_info = str(meta.get("subject") or email_detail.get("mail_subject") or email_detail.get("subject") or "")[:80]
                context_info = " ".join(str(meta.get("context") or "").split())[:140]
                print(f"   ✅ OTP TempMailMMO: {otp} | subject: {subject_info}")
                if context_info:
                    print(f"   🔎 TempMailMMO OTP context: {context_info}")
                return otp, email_detail

            preview_subject = str(latest_email.get("mail_subject") or latest_email.get("subject") or "")[:120]
            preview_text = " ".join(self._collect_text_fragments({"detail": email_detail, "list_item": latest_email}, limit=40))[:240]
            if preview_subject or preview_text:
                print(f"   🔎 Mail mới nhất chưa parse ra OTP | subject: {preview_subject}")
                if preview_text:
                    print(f"   🔎 Mail preview: {preview_text}")

            time.sleep(poll_interval)

        print(f"   ⏰ Hết thời gian chờ ({max_wait_time}s), không tìm thấy OTP")
        return None, None

    def wait_for_gpt_otp(self, max_wait_time: int = 180) -> tuple[Optional[str], Optional[dict[str, Any]]]:
        return self.wait_for_otp(
            max_wait_time=max_wait_time,
            poll_interval=4,
            sender_contains=None,
            subject_contains=None,
            require_openai=True,
            not_before=self.otp_baseline_at,
        )


def _pick_account_domain(api: TempMailMMOAPI, domain_mode: str | None = None, log_func=None) -> str | None:
    mode = (domain_mode or TMAIL_DOMAIN_MODE or "all").strip().lower()
    blocked = set(get_blocked_domains())
    if mode == "focus":
        domains = [domain for domain in get_focus_domains() if domain not in blocked]
        return random.choice(domains) if domains else None

    available = [domain for domain in api.get_domains() if domain not in blocked]
    if available:
        preferred = [domain for domain in get_focus_domains() if domain not in blocked]
        fast_hit = [domain for domain in available if domain in preferred]
        if fast_hit:
            if log_func:
                log_func(f"📧 TempMailMMO all-domain: ưu tiên domain nhanh {fast_hit[0]}...")
            return fast_hit[0]
        return random.choice(available)

    fallback = [domain for domain in get_focus_domains() if domain not in blocked]
    return random.choice(fallback) if fallback else None


def create_tempmail_account(log_func=None, domain_mode: str | None = None) -> Optional[TempMailAccount]:
    try:
        api = TempMailMMOAPI()
        domain = _pick_account_domain(api, domain_mode=domain_mode, log_func=log_func)
        mode = (domain_mode or TMAIL_DOMAIN_MODE or "all").strip().lower()
        if log_func:
            if domain:
                log_func(f"📧 Đang tạo mailbox TempMailMMO theo luồng {mode} với domain {domain}...")
            else:
                log_func(f"📧 Đang tạo mailbox TempMailMMO theo luồng {mode}...")
        email_data = api.create_email(email_domain=domain)
        if not email_data or not email_data.get("email"):
            return None
        return TempMailAccount(
            email=str(email_data["email"]).strip(),
            api=api,
            domain=str(email_data.get("email_domain") or api.email_domain or "").strip(),
            user=str(email_data.get("email_user") or api.email_user or "").strip(),
        )
    except Exception as e:
        if log_func:
            log_func(f"❌ Lỗi tạo TempMailMMO mailbox: {e}")
        return None


def wait_for_tempmail_verification_email(
    account: TempMailAccount,
    timeout: int | None = None,
    since_ts: float | None = None,
    exclude_codes: set[str] | None = None,
    baseline_message_ids: set[str] | None = None,
):
    del baseline_message_ids
    if not account or not getattr(account, "api", None):
        return None

    api = account.api
    if since_ts:
        try:
            api.otp_baseline_at = float(since_ts)
        except Exception:
            pass

    excluded = set(exclude_codes or set())
    fast_timeout = min(int(timeout or 180), 45)
    code, _detail = api.wait_for_gpt_otp(max_wait_time=fast_timeout)
    if code and code not in excluded:
        return code

    print("   🔁 TempMailMMO fallback: thử đọc OTP generic, không ép sender OpenAI")
    code, _detail = api.wait_for_otp(
        max_wait_time=max(30, min(int(timeout or 180), 90)),
        poll_interval=4,
        sender_contains=None,
        subject_contains=None,
        require_openai=False,
        not_before=getattr(api, "otp_baseline_at", None),
    )
    if code and code not in excluded:
        return code
    return None
