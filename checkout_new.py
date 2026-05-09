"""
Tao checkout trial moi truc tiep tu session ChatGPT, khong qua PetrixBot.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

import requests
from selenium.webdriver.common.by import By


SESSION_URL = "https://chatgpt.com/api/auth/session"
CHECKOUT_API_URL = "https://chatgpt.com/backend-api/payments/checkout"
PRICING_URL = "https://chatgpt.com/?promo_campaign=plus-1-month-free#pricing"
DEFAULT_TIMEOUT = 20
CHECKOUT_URL_RE = re.compile(r"https://chatgpt\.com/checkout/[^\s\"'<>]+")
HOSTED_CHECKOUT_URL_RE = re.compile(r"https://pay\.openai\.com/[^\s\"'<>]+")


def _log(log_func: Callable[[str], None] | None, message: str) -> None:
    if log_func:
        log_func(message)
    else:
        print(message)


def _read_body_text(driver) -> str:
    try:
        return driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    except Exception:
        try:
            return driver.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            return ""


def _extract_bearer_token(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        try:
            parsed = json.loads(stripped)
        except Exception:
            parsed = None
        if parsed is not None:
            return _extract_bearer_token(parsed)
        if len(stripped) > 50 and stripped.count(".") >= 2 and " " not in stripped and "{" not in stripped and "[" not in stripped:
            return stripped
        return None

    if isinstance(value, dict):
        for key in ("accessToken", "access_token", "token", "authToken", "bearerToken"):
            token = _extract_bearer_token(value.get(key))
            if token:
                return token
        for nested in value.values():
            token = _extract_bearer_token(nested)
            if token:
                return token

    if isinstance(value, list):
        for item in value:
            token = _extract_bearer_token(item)
            if token:
                return token

    return None


def _checkout_payload(country_code: str, currency: str) -> dict[str, Any]:
    return {
        "plan_name": "chatgptplusplan",
        "billing_details": {
            "country": country_code,
            "currency": currency,
        },
        "cancel_url": PRICING_URL,
        "success_url": "https://chatgpt.com/",
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": "hosted",
        "subscription_type": "plus",
        "trial_period_days": 30,
        "trial_end_action": "charge",
        "metadata": {
            "campaign": "plus-1-month-free",
            "source": "pricing_page",
        },
    }


def _extract_checkout_url(value: Any) -> str | None:
    """Tìm checkout URL dù response đổi key hoặc lồng nhiều tầng."""
    if isinstance(value, str):
        if value.startswith(("http://", "https://")) and (
            "chatgpt.com/checkout/" in value or "pay.openai.com/" in value
        ):
            return value.strip()
        match = CHECKOUT_URL_RE.search(value)
        if match:
            return match.group(0).strip()
        hosted_match = HOSTED_CHECKOUT_URL_RE.search(value)
        return hosted_match.group(0).strip() if hosted_match else None

    if isinstance(value, dict):
        for key in ("url", "checkout_url", "checkoutUrl", "link", "payment_url", "paymentUrl", "redirect_url", "redirectUrl"):
            url = _extract_checkout_url(value.get(key))
            if url:
                return url
        for nested in value.values():
            url = _extract_checkout_url(nested)
            if url:
                return url

    if isinstance(value, list):
        for item in value:
            url = _extract_checkout_url(item)
            if url:
                return url

    return None


def _normalize_checkout_result(data: Any) -> dict[str, Any]:
    checkout_url = _extract_checkout_url(data)
    if checkout_url:
        return {
            "success": True,
            "checkout_url": checkout_url,
            "raw": data,
        }
    return {
        "success": False,
        "failure_reason": f"API không trả url: {data}",
        "raw": data,
    }


def _get_access_token_from_browser_context(driver, log_func=None) -> str | None:
    _log(log_func, "   🔑 Đang lấy access token từ context browser hiện tại...")
    try:
        result = driver.execute_async_script(
            """
            const done = arguments[arguments.length - 1];

            const parseMaybeJson = (value) => {
              if (typeof value !== "string" || !value) return value;
              try {
                return JSON.parse(value);
              } catch (_err) {
                return value;
              }
            };

            const collectStorageValues = () => {
              const buckets = [];
              const sources = [window.localStorage, window.sessionStorage];
              for (const storage of sources) {
                try {
                  for (let i = 0; i < storage.length; i += 1) {
                    const key = storage.key(i);
                    buckets.push({key, value: parseMaybeJson(storage.getItem(key))});
                  }
                } catch (_err) {}
              }
              return buckets;
            };

            fetch("https://chatgpt.com/api/auth/session", {
              method: "GET",
              credentials: "include",
              headers: {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest"
              }
            })
              .then(async (resp) => {
                const text = await resp.text();
                let data;
                try {
                  data = text ? JSON.parse(text) : {};
                } catch (_err) {
                  data = {raw_text: text};
                }
                done({
                  ok: resp.ok,
                  status: resp.status,
                  data,
                  storage: collectStorageValues(),
                });
              })
              .catch((err) => done({
                ok: false,
                error: String(err),
                storage: collectStorageValues(),
              }));
            """
        )
    except Exception as e:
        _log(log_func, f"   ⚠️ Không đọc được access token từ browser context: {e}")
        return None

    token = _extract_bearer_token(result)
    if token:
        _log(log_func, f"   ✅ Đã lấy được access token từ browser context ({len(token)} ký tự)")
        return token

    if isinstance(result, dict):
        _log(log_func, f"   ⚠️ Browser context chưa trả access token hợp lệ: status={result.get('status')}")
    return None


def extract_checkout_auth_context(driver, log_func=None) -> dict[str, Any]:
    token = _get_access_token_from_browser_context(driver, log_func=log_func)
    if not token:
        token = _get_access_token_from_session(driver, log_func=log_func)

    cookies = []
    try:
        cookies = list(driver.get_cookies() or [])
    except Exception as e:
        _log(log_func, f"   ⚠️ Không đọc được cookies từ browser: {e}")

    try:
        user_agent = driver.execute_script("return navigator.userAgent || ''") or ""
    except Exception:
        user_agent = ""

    return {
        "token": token or "",
        "cookies": cookies,
        "user_agent": user_agent or "Mozilla/5.0",
    }


def _browser_fetch_checkout(driver, payload: dict[str, Any], log_func=None) -> dict[str, Any]:
    _log(log_func, "   🌐 Đang gọi checkout API trực tiếp trong browser...")
    bearer_token = _get_access_token_from_browser_context(driver, log_func=log_func)
    result = driver.execute_async_script(
        """
        const payload = arguments[0];
        const bearerToken = arguments[1];
        const done = arguments[arguments.length - 1];

        const headers = {
          "Content-Type": "application/json",
          "Origin": "https://chatgpt.com",
          "Referer": "https://chatgpt.com/?promo_campaign=plus-1-month-free#pricing"
        };
        if (bearerToken) {
          headers["Authorization"] = `Bearer ${bearerToken}`;
        }

        fetch("https://chatgpt.com/backend-api/payments/checkout", {
          method: "POST",
          credentials: "include",
          headers,
          body: JSON.stringify(payload)
        })
          .then(async (resp) => {
            const text = await resp.text();
            let data;
            try {
              data = text ? JSON.parse(text) : {};
            } catch (_err) {
              data = {raw_text: text};
            }
            done({ok: resp.ok, status: resp.status, data});
          })
          .catch((err) => done({ok: false, error: String(err)}));
        """,
        payload,
        bearer_token,
    )

    if not isinstance(result, dict):
        return {"success": False, "failure_reason": f"Browser fetch trả dữ liệu lạ: {result}"}

    if result.get("ok"):
        return _normalize_checkout_result(result.get("data"))

    error = result.get("error") or result.get("data") or result
    return {
        "success": False,
        "failure_reason": f"Browser fetch checkout lỗi: {error}",
        "raw": result,
    }


def _get_access_token_from_session(driver, log_func=None) -> str | None:
    token = _get_access_token_from_browser_context(driver, log_func=log_func)
    if token:
        return token

    _log(log_func, "   🔑 Đang lấy access token từ session ChatGPT...")
    original_url = ""
    try:
        original_url = driver.current_url
    except Exception:
        pass

    try:
        driver.get(SESSION_URL)
        deadline = time.time() + 6
        while time.time() < deadline:
            body_text = _read_body_text(driver)
            if body_text:
                try:
                    data = json.loads(body_text)
                except json.JSONDecodeError:
                    time.sleep(0.2)
                    continue
                token = _extract_bearer_token(data)
                if token:
                    _log(log_func, f"   ✅ Đã lấy được access token từ session page ({len(token)} ký tự)")
                    return token
            time.sleep(0.2)

        token = driver.execute_script(
            """
            return (
              localStorage.getItem("accessToken")
              || localStorage.getItem("auth-token")
              || localStorage.getItem("token")
              || ""
            );
            """
        )
        token = _extract_bearer_token(token)
        if token:
            _log(log_func, f"   ✅ Đã lấy được access token từ storage ({len(token)} ký tự)")
            return token
        return None
    finally:
        try:
            if original_url and driver.current_url != original_url:
                driver.get(original_url)
        except Exception:
            pass


def _requests_checkout(driver, payload: dict[str, Any], log_func=None) -> dict[str, Any]:
    token = _get_access_token_from_session(driver, log_func=log_func)
    if not token:
        return {"success": False, "failure_reason": "Không lấy được access token"}

    _log(log_func, "   🌐 Fallback gọi checkout API bằng requests...")
    session = requests.Session()
    try:
        user_agent = driver.execute_script("return navigator.userAgent || ''") or ""
    except Exception:
        user_agent = ""

    try:
        for cookie in driver.get_cookies():
            name = cookie.get("name")
            value = cookie.get("value")
            if name and value is not None:
                session.cookies.set(name, value, domain=cookie.get("domain"), path=cookie.get("path") or "/")
    except Exception as e:
        _log(log_func, f"   ⚠️ Không copy được cookies từ browser sang requests: {e}")

    resp = session.post(
        CHECKOUT_API_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": PRICING_URL,
            "User-Agent": user_agent or "Mozilla/5.0",
        },
        timeout=DEFAULT_TIMEOUT,
    )

    try:
        data = resp.json()
    except Exception:
        data = {"raw_text": resp.text[:1000]}

    if resp.ok:
        return _normalize_checkout_result(data)

    return {
        "success": False,
        "failure_reason": f"Checkout API lỗi HTTP {resp.status_code}: {data}",
        "raw": data,
    }


def create_trial_checkout_from_auth_context(
    auth_context: dict[str, Any],
    country_code: str = "ID",
    currency: str = "IDR",
    log_func=None,
) -> dict[str, Any]:
    payload = _checkout_payload(country_code=country_code, currency=currency)
    token = str((auth_context or {}).get("token") or "").strip()
    if not token:
        return {"success": False, "failure_reason": "Không có access token trong auth_context"}

    _log(log_func, "   🌐 Đang gọi checkout API song song bằng requests-only...")
    session = requests.Session()
    for cookie in list((auth_context or {}).get("cookies") or []):
        name = cookie.get("name")
        value = cookie.get("value")
        if name and value is not None:
            session.cookies.set(name, value, domain=cookie.get("domain"), path=cookie.get("path") or "/")

    resp = session.post(
        CHECKOUT_API_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": PRICING_URL,
            "User-Agent": str((auth_context or {}).get("user_agent") or "Mozilla/5.0"),
        },
        timeout=DEFAULT_TIMEOUT,
    )

    try:
        data = resp.json()
    except Exception:
        data = {"raw_text": resp.text[:1000]}

    if resp.ok:
        result = _normalize_checkout_result(data)
        if result.get("success"):
            _log(log_func, "   ✅ Đã lấy được checkout link trial mới từ luồng song song")
            _log(log_func, f"   🔗 {result['checkout_url']}")
        return result

    return {
        "success": False,
        "failure_reason": f"Checkout API lỗi HTTP {resp.status_code}: {data}",
        "raw": data,
    }


def create_trial_checkout(driver, country_code: str = "ID", currency: str = "IDR", log_func=None) -> dict[str, Any]:
    """
    Tao link checkout trial moi.

    Uu tien fetch trong browser de giong userscript nhat; neu loi thi fallback qua requests.
    """
    payload = _checkout_payload(country_code=country_code, currency=currency)

    browser_result = _browser_fetch_checkout(driver, payload, log_func=log_func)
    if browser_result.get("success"):
        _log(log_func, "   ✅ Đã lấy được checkout link trial mới từ browser")
        _log(log_func, f"   🔗 {browser_result['checkout_url']}")
        return browser_result

    _log(log_func, f"   ⚠️ Browser checkout chưa thành công: {browser_result.get('failure_reason')}")

    try:
        requests_result = _requests_checkout(driver, payload, log_func=log_func)
    except Exception as e:
        return {
            "success": False,
            "failure_reason": f"Lỗi fallback requests checkout: {e}",
        }

    if requests_result.get("success"):
        _log(log_func, "   ✅ Đã lấy được checkout link trial mới qua requests")
        _log(log_func, f"   🔗 {requests_result['checkout_url']}")
    return requests_result
