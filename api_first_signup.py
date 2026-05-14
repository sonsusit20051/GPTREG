"""
Luồng đăng ký ChatGPT theo hướng API-first để giảm phụ thuộc vào fingerprint browser mới tinh.
Tham khảo từ reg.py do user cung cấp và điều chỉnh để dùng chung với Hotmail OAuth hiện có.
"""

from __future__ import annotations

import random
import re
import string
import time
import uuid
from datetime import datetime
from typing import Any

import requests

from email_service import HotmailAccount, wait_for_verification_email


API_FIRST_PASSWORD_VERIFY_URL = "https://auth.openai.com/api/accounts/password/verify"
API_FIRST_REGISTER_URL = "https://auth.openai.com/api/accounts/user/register"
API_FIRST_SEND_OTP_URL = "https://auth.openai.com/api/accounts/email-otp/send"
API_FIRST_VALIDATE_OTP_URL = "https://auth.openai.com/api/accounts/email-otp/validate"
API_FIRST_CREATE_ACCOUNT_URL = "https://auth.openai.com/api/accounts/create_account"
API_FIRST_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


def _response_preview(response: requests.Response, limit: int = 180) -> str:
    try:
        text = str(response.text or "")
    except Exception:
        text = ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _parse_json_response(response: requests.Response, label: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        content_type = str(response.headers.get("content-type") or "").strip()
        preview = _response_preview(response)
        raise RuntimeError(
            f"{label} không trả JSON hợp lệ "
            f"(status={response.status_code}, content-type={content_type or 'unknown'}, preview={preview or 'empty'})"
        )
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} trả JSON không đúng dạng object")
    return data


def _extract_state_from_text(value: str) -> str:
    match = re.search(r"(?:[?&]|^)state=([^&]+)", str(value or ""))
    return match.group(1) if match else ""


def _random_birthdate() -> str:
    age = random.randint(18, 40)
    year = datetime.now().year - age
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}"


def _build_trace_headers(referer: str, trace_id: int) -> dict[str, str]:
    parent_id = random.getrandbits(63)
    return {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "accept-encoding": "gzip, deflate, br",
        "referer": referer,
        "content-type": "application/json",
        "origin": "https://auth.openai.com",
        "priority": "u=1, i",
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "traceparent": f"00-0000000000000000{trace_id:016x}-{parent_id:016x}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": str(parent_id),
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": str(trace_id),
    }


def _sentinel_tokens(device_id: str) -> tuple[str, str]:
    so_token = (
        '{"so":"ShEcBRcDERQWeFZiT3ZeWnFmZgF0cH9xfXlUf3h4VkBPFhUTBx0aDgkRFBZ6YA4TFhUTBxsaDAARFBZ4YA4TFhUTCh0aDQARFBZ4YA4TFhUTBhYaDxMJDHV7AVFsZQF4cWxDfHJEf3VxYGJndXh4VwwYGwYHAAcLEwkMdWgMDgwYGwUKAAELEwkMdWgMDgwYGwQFAAEMEwkMdWgMDgwYGwAKAAUIEwkMdXhgaW9DXnJxVlt3dWJjcX5GDhMWFRMGGhoMAhEUFnhwYnR1TlZwbExgcnJ/YXx2RBMJGx0RHwEXAQIMDhtwcl1ReHALbXxrQg4MGBsFAAAMDxMJDHZ4DA4MGBsJBwAHDhMJDHV4XlZvdQQMEQIWDQQdFgMbCxFvU3BrckZXfnBLGnF6YHZqc158Y21leGsRAhYABx0fDBsLEW9DWgwRAhYNCB0ZAhsLEW92CWZ3bwx9cEsednhgUGlzaHxxb1N0aBECFgoBHRsNGwsRb3V4cHJ/BH1wWx53elZQbXJOAXxvQ0pmEQIWDgUdGwEbCxFvZglQcUlDfHBxFnp6cGZick5SY291aAwRUw==","c":"gAAAAABp7OufCCpXqfPCDnz9vCXf6ouek2m9b6r79QP3J_j3YGa8L8BABu_MMQhvLPBBDM4AmaRdXIk51e1wbclHbI_r2nMWlrZW0HT_D6d75B8kc2Vrk5Uqgphe2TRYvhxs7RG5XCvbWcbfjA7WtGmEJQSGlEs-qmQCfsY7_dhhL2HdnGHde-HU9vimKHtlkpdGifFd6T8iu292dU3awmZ3gfXnTe0JKGuMD18tRLROqlzWJ0zQK-XVQGaMJ-gGpuAmqIYcMFzJZPfug7sEoc1incyjB17r6abWS3_7-PL8VrQOByLGngO5_zJ6BIQm33GuIFlhWvXczrrFoT3Gq5_eSHXa0BYYG5TtR29FF1XQXdDFWCYdlOnToUkMyo1hF_819ImD8YhNMFnO6XxqULw0SM065z6kT8NEXbZgOrQ_ZUw_BgupqwFzgyNyWKENd4fLxgbDKcQ4-ni_Up7hkiPVgmBawsfRDrUHHwvjkix5EB25egwiZWevoMYrF8_d7QHh1U57XUz1s0WZYrIOEGmie8M-N6cQvtOcqv3waydCnv0f0yJabVDSMCPD99FcqAS06cR4B4tHenup8FgYyT74j0dhcBCGPWuUXXIX-bhnySeKSvg3Gqd06i-yjV63OnjDQ0zM4ocWnBlRk14S8EOaMrb2EllPn6h6G0DwCSz5BamzqoqD9pbzzo_dWs49tqjAxBgyfJHzL-oEI3buCJxCMkNdAAHwZ6fqzVJwIUuzu5EIe-vpgEB69J9MNjHV6Nv5-S8Pls5WRbII-XhhvVy75JQUrKP17B0v1FOSTvWKV782XJap6_yncREf30dViwwtTj0fReiehPZ1T-EKWHYB2YheTx80BPN-q1QunMMzFCYVq_cbMh7STe5GLNAy9VyeXEghUgshbfF3WflslfF4_2iNXjR_h_riQAmi7GHIEMc4GHLR5sguosG6YubGMK7Ze_ioWOJQK9UoPY3PWefgbIqj2zhcqIGxuL6DeB08nHKPi16oyZ7Tm1MthBgy_TdYFodLWUsBu8L9M8l7nOk9SdOF8CrvaPTqXRJhwfw-wgeesbkOLTXFSE1fgccwY1a2raNqJEBJz9_Oigl4CV_PYr8ZvjLh-FO_LNMhWXe2ud9nQGOpGx9Q-BgdaqdMzIWDXG34zLEq0EmWv_kEdJ1gIFjnEwy9NNBFL2KEvQ6qRcfTSOGDbHOpQQlH3dGtfvSo2DdnpNT-19BmtOqiOQhkw6i65ksagPJfp9aOKWbLT6VX4EIiJYTt1IUtmx15ACPaEmt9mQz_j-mryD61kwn2utQjoGYfgb8DPdZgrXdXF3oWJeQF5-ySaempwyRMNpgDO-C_lXKJtlc52tb1CaOkfHw1IbFNR9BdXm-APlrVI2VLhSWmGobFb5oed2-TFxhZ4sOL2hSVK8SOc45Fkxj5BViDFNJ8KX9dPAd--EJ_sLprpBno7RefKCn5mEn_lm5B9lZnSlZgqoUlMinu9uhi7xomf4gN3-9ayl9sRJZ-dKbHgyTKR5jVScohfcirE9aidwyHQkE4_LCYvyA9IdhGGe4X6vVI7fmH3netVQGIaeHXt6K-v331e2vZSsetdILSv29Y0FzBdOLz3aS5tBur_UwP8jjgzopw-r_LR_APEAHtI34veZr6tXiEKE1PlzFC-juU2gqrGXw6aljipVMkbJ6U0nrrq200epr_U6p2uGYWzqDrmF1KAOtKrmrYx-TvPWeIMbBUpNipxmC7HX91ol-KdB2CiD8ZC2h9QUW_lRGfTgwDXj055_jvc4POJerl_CPM8z1Y3jANu9Y5YcxzDG80Bq8xQI9TWJFJ8kzQpT9lBIDDya_K1l4N3ANa5_ecoCmdryGCU-HWy1qjMKsxoJ85D2X1keI8jsk_6xQsMaihQf7cXmO5ncSCH7JistfJ_BBaiAQObYNUpHjOksfuYOXcnyqMhORN4W7ValG-SMZOOgpyAtT0kbpKb9zzL7Hb5dQtbk2JHcRbfvDBEvBtQe0LypMA8pXpP3KrOcGOa46XOufPyM50eLKlSN2qj0hthVl6EZQ0W4ScdaCnKxdT5tXjil_aAQ==","id":"'
        + device_id
        + '","flow":"oauth_create_account"}'
    )
    s_token = (
        '{"p":"gAAAAABWzIxMzQsIlNhdCBBcHIgMjUgMjAyNiAyMzozNDoyOCBHTVQrMDcwMCAoSW5kb2NoaW5hIFRpbWUpIiwyMjQ4MTQ2OTQ0LDEwNywiTW96aWxsYS81LjAgKFdpbmRvd3MgTlQgMTAuMDsgV2luNjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3LjM2IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzE0Ny4wLjAuMCBTYWZhcmkvNTM3LjM2IiwiY2hyb21lLWV4dGVuc2lvbjovL2VwcGlvY2VtaG1ubGJoanBsY2drb2ZjaWllZ29tY29uL2xpYnMvcmVxdWVzdHMuanMiLG51bGwsImVuLVVTIiwiZW4tVVMsZW4iLDU0LCJyZXF1ZXN0TWVkaWFLZXlTeXN0ZW1BY2Nlc3PiiJJmdW5jdGlvbiByZXF1ZXN0TWVkaWFLZXlTeXN0ZW1BY2Nlc3MoKSB7IFtuYXRpdmUgY29kZV0gfSIsImxvY2F0aW9uIiwiY2FuY2VsQW5pbWF0aW9uRnJhbWUiLDM4ODUzMC43OTk5OTk5NTIzLCIxODdkMjIxOC02M2MwLTRkYWUtOTVjZS00MGNiNTQ0MDEwYjMiLCIiLDgsMTc3NzEzNDQ3OTk2Mi43LDAsMCwwLDAsMCwwLDBd~S","t":"ShIdDBYBCAwOGnBiGlB6cFdsdXoFdW11fXB4SWVxcGFvUXlwDRMWFBMDAAUMEwoMZVNjYH9xTGhmWXZgZmZkbWsAcXlifWtgeXEJYGJrUFpnW2dRcHBhYnVpcFJvUwh0cW9+cGV2bHFgZwFIfAl/YHgFemRmb3ZqZVxsV28AZmNjfghxfExhcHFGU3xwcWd8cHBTY3xtf1F4YUByZ3tfWWYBbGBqXXZNfAlnaXhianRleERcZwEXcHlwZWd2X1ZWamVXe3FGBGFiAHhmb1pUfGJQBH54YkxrZntbWWllSmZpWnZ0Zm5dcnlfS1dybAB9cmFNfX9gdWV3aklUf3JUd2EeUGx3XGRmbGdISmJqBHx/WFRVYnsNeWRmG1FgY19ldl9WamxTCHtyVl9wd3VkcWwBcmJjflJxeXJUcmJmcmJnAXhua3cBSmJvCFF2Ym5keEVAe2ZmH3JsAXJ8bFQEVGlxTHdleERwZGZ3UWxnZkxjbmd7aVgJeWV7fnpmZXhuaWdAYnxPeFRvdQB5cWxXdnJHY1dgZHJ0Zm1/eHtMYmJlewVbaWZ7UWsBenRsbWdgf3J+VGhCBFlyR0l3f0YAYXZfYFZqdmpyYmsNeWIBRmx5dFRKZVN/eHkFYkRne1tZdXF7cX9GR2N1X3BVaXFMd2V4RHBkZndRbGdmTGNuZ3tpWAl5ZXt+emZleG5lYh5QaWV2RWd+cGljfE9gVW9TV3R4b0xbaWZGbGpacU93T3RoaXZId2gfAG9wV39zcHNfbHVfUlJpdkh1Z3tYW2VyY39wcHF5dmlwaWxTCFV1RUxaZXV4UmlJaW12eVZWaVN9cnJWR2F2XB9ia3RxT3dPc2QMGBoJBQAGABMKDGdQe2B/WFRoaGZYXGl1eG18cARkdV9oaWl2SGBiRQ1cZ3hCYmoAfn9manhlb0NXc3hvTG5lXGxuaV9YSmIIYH1qU19VdUUNe2ZmRlddXURncU9eVWl2SGJoHn5aYl54YmBacn9xT15UbFxpZWdrQFxpAUJxfHAEfnZPVnNoXwFoZmhEW3ZXXXZwYHlhfGp7VXZhemRoaURqYnV4bXxwBGJ1CAENDBgaAgMABQwTCgxVUHtAeQVcZmhrR21SZhdgeXNpe3EKVXh5U0t1dUl9dXdxaEAaTA==","c":"gAAAAABp7OufCCpXqfPCDnz9vCXf6ouek2m9b6r79QP3J_j3YGa8L8BABu_MMQhvLPBBDM4AmaRdXIk51e1wbclHbI_r2nMWlrZW0HT_D6d75B8kc2Vrk5Uqgphe2TRYvhxs7RG5XCvbWcbfjA7WtGmEJQSGlEs-qmQCfsY7_dhhL2HdnGHde-HU9vimKHtlkpdGifFd6T8iu292dU3awmZ3gfXnTe0JKGuMD18tRLROqlzWJ0zQK-XVQGaMJ-gGpuAmqIYcMFzJZPfug7sEoc1incyjB17r6abWS3_7-PL8VrQOByLGngO5_zJ6BIQm33GuIFlhWvXczrrFoT3Gq5_eSHXa0BYYG5TtR29FF1XQXdDFWCYdlOnToUkMyo1hF_819ImD8YhNMFnO6XxqULw0SM065z6kT8NEXbZgOrQ_ZUw_BgupqwFzgyNyWKENd4fLxgbDKcQ4-ni_Up7hkiPVgmBawsfRDrUHHwvjkix5EB25egwiZWevoMYrF8_d7QHh1U57XUz1s0WZYrIOEGmie8M-N6cQvtOcqv3waydCnv0f0yJabVDSMCPD99FcqAS06cR4B4tHenup8FgYyT74j0dhcBCGPWuUXXIX-bhnySeKSvg3Gqd06i-yjV63OnjDQ0zM4ocWnBlRk14S8EOaMrb2EllPn6h6G0DwCSz5BamzqoqD9pbzzo_dWs49tqjAxBgyfJHzL-oEI3buCJxCMkNdAAHwZ6fqzVJwIUuzu5EIe-vpgEB69J9MNjHV6Nv5-S8Pls5WRbII-XhhvVy75JQUrKP17B0v1FOSTvWKV782XJap6_yncREf30dViwwtTj0fReiehPZ1T-EKWHYB2YheTx80BPN-q1QunMMzFCYVq_cbMh7STe5GLNAy9VyeXEghUgshbfF3WflslfF4_2iNXjR_h_riQAmi7GHIEMc4GHLR5sguosG6YubGMK7Ze_ioWOJQK9UoPY3PWefgbIqj2zhcqIGxuL6DeB08nHKPi16oyZ7Tm1MthBgy_TdYFodLWUsBu8L9M8l7nOk9SdOF8CrvaPTqXRJhwfw-wgeesbkOLTXFSE1fgccwY1a2raNqJEBJz9_Oigl4CV_PYr8ZvjLh-FO_LNMhWXe2ud9nQGOpGx9Q-BgdaqdMzIWDXG34zLEq0EmWv_kEdJ1gIFjnEwy9NNBFL2KEvQ6qRcfTSOGDbHOpQQlH3dGtfvSo2DdnpNT-19BmtOqiOQhkw6i65ksagPJfp9aOKWbLT6VX4EIiJYTt1IUtmx15ACPaEmt9mQz_j-mryD61kwn2utQjoGYfgb8DPdZgrXdXF3oWJeQF5-ySaempwyRMNpgDO-C_lXKJtlc52tb1CaOkfHw1IbFNR9BdXm-APlrVI2VLhSWmGobFb5oed2-TFxhZ4sOL2hSVK8SOc45Fkxj5BViDFNJ8KX9dPAd--EJ_sLprpBno7RefKCn5mEn_lm5B9lZnSlZgqoUlMinu9uhi7xomf4gN3-9ayl9sRJZ-dKbHgyTKR5jVScohfcirE9aidwyHQkE4_LCYvyA9IdhGGe4X6vVI7fmH3netVQGIaeHXt6K-v331e2vZSsetdILSv29Y0FzBdOLz3aS5tBur_UwP8jjgzopw-r_LR_APEAHtI34veZr6tXiEKE1PlzFC-juU2gqrGXw6aljipVMkbJ6U0nrrq200epr_U6p2uGYWzqDrmF1KAOtKrmrYx-TvPWeIMbBUpNipxmC7HX91ol-KdB2CiD8ZC2h9QUW_lRGfTgwDXj055_jvc4POJerl_CPM8z1Y3jANu9Y5YcxzDG80Bq8xQI9TWJFJ8kzQpT9lBIDDya_K1l4N3ANa5_ecoCmdryGCU-HWy1qjMKsxoJ85D2X1keI8jsk_6xQsMaihQf7cXmO5ncSCH7JistfJ_BBaiAQObYNUpHjOksfuYOXcnyqMhORN4W7ValG-SMZOOgpyAtT0kbpKb9zzL7Hb5dQtbk2JHcRbfvDBEvBtQe0LypMA8pXpP3KrOcGOa46XOufPyM50eLKlSN2qj0hthVl6EZQ0W4ScdaCnKxdT5tXjil_aAQ==","id":"'
        + device_id
        + '","flow":"oauth_create_account"}'
    )
    return so_token, s_token


def register_account_via_api(
    account: HotmailAccount,
    password: str,
    *,
    otp_timeout: int = 60,
    log_func=print,
) -> dict[str, Any]:
    device_id = str(uuid.uuid4())
    trace_id = random.getrandbits(63)
    login_hint = account.email.replace("@", "%40")

    session = requests.Session()
    session.headers.update(
        {
            "user-agent": API_FIRST_USER_AGENT,
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            # Tránh xin brotli ở nhánh requests thường để khỏi gặp body JSON bị decode thành bytes rác.
            "accept-encoding": "gzip, deflate",
            "cache-control": "no-cache",
            "pragma": "no-cache",
        }
    )
    session.cookies.set("oai-did", device_id, domain=".openai.com")

    try:
        log_func("🌐 API-first: lấy CSRF token...")
        csrf_resp = session.get(
            "https://chatgpt.com/api/auth/csrf",
            headers={"accept": "application/json, text/plain, */*"},
            timeout=30,
        )
        csrf_resp.raise_for_status()
        csrf_data = _parse_json_response(csrf_resp, "CSRF endpoint")
        csrf_token = str(csrf_data.get("csrfToken") or "").strip()
        if not csrf_token:
            return {"success": False, "reason": "Không lấy được CSRF token"}

        log_func("🌐 API-first: mở luồng signup OAuth...")
        logging_id = str(uuid.uuid4())
        signin_url = (
            "https://chatgpt.com/api/auth/signin/openai?"
            f"prompt=signup&ext-oai-did={device_id}&auth_session_logging_id={logging_id}"
            f"&ext-passkey-client-capabilities=0101&screen_hint=signup&login_hint={login_hint}"
        )
        signin_resp = session.post(
            signin_url,
            headers={
                "accept": "application/json, text/plain, */*",
                "content-type": "application/x-www-form-urlencoded",
                "referer": "https://chatgpt.com/",
            },
            data=f"callbackUrl=https://chatgpt.com/&csrfToken={csrf_token}&json=true",
            timeout=30,
        )
        signin_resp.raise_for_status()
        signin_data = _parse_json_response(signin_resp, "Signin endpoint")
        authorize_url = str(signin_data.get("url") or "").strip()
        if not authorize_url:
            return {"success": False, "reason": "Không lấy được authorize URL"}

        oauth_resp = session.get(authorize_url, timeout=45, allow_redirects=True)
        oauth_resp.raise_for_status()
        final_url = str(oauth_resp.url or "").strip()
        state = _extract_state_from_text(authorize_url) or _extract_state_from_text(final_url)
        if not state:
            for hist in list(getattr(oauth_resp, "history", []) or []):
                state = _extract_state_from_text(str(getattr(hist, "url", "") or ""))
                if state:
                    break
        if not state:
            history_urls = [str(getattr(item, "url", "") or "").strip() for item in list(getattr(oauth_resp, "history", []) or [])]
            details = [f"authorize_url={authorize_url}", f"final_url={final_url}"]
            if history_urls:
                details.append("history=" + " -> ".join(history_urls[:5]))
            return {"success": False, "reason": "Không lấy được state OAuth | " + " | ".join(details)}

        trace_headers = _build_trace_headers(final_url, trace_id)
        log_func("🌐 API-first: tạo account bằng email + password...")
        verify_resp = session.post(
            API_FIRST_PASSWORD_VERIFY_URL,
            headers=trace_headers,
            json={"password": password},
            timeout=30,
        )
        if verify_resp.status_code != 200:
            register_resp = session.post(
                API_FIRST_REGISTER_URL,
                headers=trace_headers,
                json={"password": password, "username": account.email},
                timeout=30,
            )
            if register_resp.status_code != 200:
                return {
                    "success": False,
                    "reason": f"API register fail {register_resp.status_code}",
                }

        verify_page_url = f"https://auth.openai.com/email-verification?state={state}"
        session.get(
            verify_page_url,
            headers={"referer": "https://auth.openai.com/"},
            timeout=20,
        )
        send_headers = _build_trace_headers(verify_page_url, trace_id)
        log_func("📨 API-first: yêu cầu gửi OTP email...")
        send_resp = session.post(
            API_FIRST_SEND_OTP_URL,
            headers=send_headers,
            json={},
            timeout=20,
        )
        if send_resp.status_code not in (200, 204):
            return {"success": False, "reason": f"API send OTP fail {send_resp.status_code}"}

        otp_since_ts = time.time()
        otp_code = wait_for_verification_email(account, timeout=otp_timeout, since_ts=otp_since_ts)
        if not otp_code:
            return {"success": False, "reason": "API-first không lấy được OTP hợp lệ"}

        log_func("🔢 API-first: xác minh OTP...")
        otp_resp = session.post(
            API_FIRST_VALIDATE_OTP_URL,
            headers=_build_trace_headers(verify_page_url, trace_id),
            json={"code": otp_code},
            timeout=30,
        )
        if otp_resp.status_code != 200:
            return {"success": False, "reason": f"API validate OTP fail {otp_resp.status_code}"}

        session.get(
            "https://auth.openai.com/api/accounts/user",
            headers=_build_trace_headers("https://auth.openai.com/about-you", trace_id),
            timeout=15,
        )
        session.get(
            "https://auth.openai.com/about-you",
            headers={"referer": "https://auth.openai.com/email-verification"},
            timeout=15,
        )
        session.cookies.set("rg_context", "prim", domain=".openai.com")
        session.cookies.set("iss_context", "default", domain=".openai.com")

        profile_headers = _build_trace_headers("https://auth.openai.com/about-you", trace_id)
        so_token, sentinel_token = _sentinel_tokens(device_id)
        profile_headers.update(
            {
                "openai-sentinel-so-token": so_token,
                "openai-sentinel-token": sentinel_token,
            }
        )
        display_name = "".join(random.choices(string.ascii_letters, k=8)).capitalize()
        log_func("🧾 API-first: tạo profile cơ bản...")
        profile_resp = session.post(
            API_FIRST_CREATE_ACCOUNT_URL,
            headers=profile_headers,
            json={"name": display_name, "birthdate": _random_birthdate()},
            timeout=30,
        )
        if profile_resp.status_code not in (200, 302):
            return {
                "success": False,
                "reason": f"API create_account fail {profile_resp.status_code}",
            }

        return {
            "success": True,
            "email": account.email,
            "password": password,
            "otp_code": otp_code,
            "display_name": display_name,
        }
    except requests.RequestException as e:
        return {"success": False, "reason": f"API-first lỗi HTTP: {e}"}
    except Exception as e:
        return {"success": False, "reason": f"API-first lỗi: {e}"}
