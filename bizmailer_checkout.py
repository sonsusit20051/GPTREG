"""
Tạo link trial GoPay qua Bizmailer API.
Giữ checkout_new.py nguyên trạng, chỉ thay luồng gọi ở main.py.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import requests


BIZMAILER_API_KEY = "tgpay_6yQVftuIPqutZr-GNs8NelYC00PhtJ6i"
BIZMAILER_LINK_URL = "https://bizmailer.org/api/trial-gopay-link"
BIZMAILER_BALANCE_URL = "https://bizmailer.org/api/trial-gopay-balance"
SESSION_URL = "https://chatgpt.com/api/auth/session"
DEFAULT_TIMEOUT = 30


def _log(log_func: Callable[[str], None] | None, message: str) -> None:
    if log_func:
        log_func(message)
    else:
        print(message)


def _normalize_result(data: Any) -> dict[str, Any]:
    if isinstance(data, dict) and data.get("ok") and data.get("url"):
        return {
            "success": True,
            "checkout_url": str(data.get("url") or "").strip(),
            "charged": data.get("charged"),
            "balance_after": data.get("balance_after"),
            "raw": data,
        }
    return {
        "success": False,
        "failure_reason": f"Bizmailer không trả url hợp lệ: {data}",
        "raw": data,
    }


def _fetch_auth_session_json(driver, log_func=None) -> str | None:
    _log(log_func, "   🔑 Đang lấy full JSON /api/auth/session cho Bizmailer...")
    try:
        result = driver.execute_async_script(
            """
            const done = arguments[arguments.length - 1];
            fetch(arguments[0], {
              method: 'GET',
              credentials: 'include',
              headers: {
                'Accept': 'application/json',
                'X-Requested-With': 'XMLHttpRequest'
              }
            })
              .then(async (resp) => {
                const text = await resp.text();
                done({ok: resp.ok, status: resp.status, text});
              })
              .catch((err) => done({ok: false, error: String(err)}));
            """,
            SESSION_URL,
        )
    except Exception as e:
        _log(log_func, f"   ⚠️ Không fetch được auth session trong browser: {e}")
        return None

    if not isinstance(result, dict):
        return None
    text = str(result.get("text") or "").strip()
    if not text:
        _log(log_func, f"   ⚠️ Auth session trống: {result}")
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        _log(log_func, "   ⚠️ Auth session không phải JSON hợp lệ")
        return None
    if not isinstance(parsed, dict):
        _log(log_func, "   ⚠️ Auth session không phải object JSON")
        return None
    _log(log_func, "   ✅ Đã lấy full JSON auth session")
    return json.dumps(parsed, ensure_ascii=False)


def extract_bizmailer_context(driver, log_func=None) -> dict[str, Any]:
    raw_data = _fetch_auth_session_json(driver, log_func=log_func) or ""
    stripe_url = ""
    try:
        current_url = str(driver.current_url or "").strip()
        if current_url.startswith("https://pay.openai.com/"):
            stripe_url = current_url
    except Exception:
        pass
    return {
        "raw_data": raw_data,
        "stripe_url": stripe_url,
    }


def get_bizmailer_balance(log_func=None) -> dict[str, Any]:
    _log(log_func, "   💰 Đang kiểm tra số dư Bizmailer...")
    resp = requests.get(
        BIZMAILER_BALANCE_URL,
        headers={"X-API-Key": BIZMAILER_API_KEY},
        timeout=DEFAULT_TIMEOUT,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"raw_text": resp.text[:1000]}
    return {
        "success": bool(resp.ok),
        "status_code": resp.status_code,
        "data": data,
    }


def create_trial_checkout_from_bizmailer_context(
    context: dict[str, Any],
    log_func=None,
) -> dict[str, Any]:
    payload = {}
    raw_data = str((context or {}).get("raw_data") or "").strip()
    stripe_url = str((context or {}).get("stripe_url") or "").strip()

    if raw_data:
        payload["raw_data"] = raw_data
    elif stripe_url:
        payload["stripe_url"] = stripe_url
    else:
        return {"success": False, "failure_reason": "Không có raw_data hoặc stripe_url cho Bizmailer"}

    _log(log_func, "   🌐 Đang gọi Bizmailer Trial GoPay API...")
    resp = requests.post(
        BIZMAILER_LINK_URL,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": BIZMAILER_API_KEY,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"raw_text": resp.text[:1000]}

    if resp.ok:
        result = _normalize_result(data)
        if result.get("success"):
            _log(log_func, "   ✅ Bizmailer đã trả trial GoPay link")
            _log(log_func, f"   🔗 {result['checkout_url']}")
        return result

    return {
        "success": False,
        "failure_reason": f"Bizmailer lỗi HTTP {resp.status_code}: {data}",
        "raw": data,
    }


def create_trial_checkout_via_bizmailer(driver, log_func=None) -> dict[str, Any]:
    context = extract_bizmailer_context(driver, log_func=log_func)
    return create_trial_checkout_from_bizmailer_context(context, log_func=log_func)
