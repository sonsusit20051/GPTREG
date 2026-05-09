"""
Lay checkout link qua PetrixBot API sau khi tai khoan da dang nhap ChatGPT.

Flow:
  1. Truy cap https://chatgpt.com/api/auth/session bang Selenium.
  2. Doc toan bo body JSON cua trang session.
  3. Lay accessToken tu JSON do.
  4. Goi PetrixBot API voi currency IDR de lay checkout link.
"""

import json
import re
import time

import requests
from selenium.webdriver.common.by import By

PETRIX_BASE = "https://ezweystock.petrix.id/gpt"
PETRIX_PAYMENT = f"{PETRIX_BASE}/payment"
PETRIX_CURRENCY = f"{PETRIX_BASE}/currency"

DEFAULT_TIMEOUT = 30
SESSION_URL = "https://chatgpt.com/api/auth/session"
CHATGPT_HOME_URL = "https://chatgpt.com/"
CHECKOUT_URL_RE = re.compile(r"https://chatgpt\.com/checkout/[^\s\"'<>]+")


def _log(log_func, msg):
    if log_func:
        log_func(msg)
    else:
        print(msg)


def _read_body_text(driver):
    """Doc body text cua trang hien tai bang Selenium."""
    try:
        return driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    except Exception:
        try:
            return driver.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            return ""


def get_session_json_from_browser(driver, max_retries=3, log_func=None):
    """
    Truy cap endpoint session va parse toan bo JSON response.

    Tra ve:
        tuple(dict|None, str): (session_data, raw_body_text)
    """
    last_body = ""

    for attempt in range(max_retries):
        try:
            _log(log_func, f"   🔑 Đang mở ChatGPT session API... (lần {attempt + 1}/{max_retries})")
            _log(log_func, f"   [Debug] URL hiện tại: {driver.current_url}")

            driver.get(SESSION_URL)
            deadline = time.time() + 6
            while time.time() < deadline:
                body_text = _read_body_text(driver)
                last_body = body_text

                if body_text:
                    try:
                        data = json.loads(body_text)
                    except json.JSONDecodeError:
                        time.sleep(0.2)
                        continue

                    if isinstance(data, dict):
                        if data.get("accessToken"):
                            _log(log_func, f"   ✅ Đã copy session và thấy accessToken ({len(body_text)} chars)")
                            return data, body_text
                        if data.get("user") or data.get("account"):
                            _log(log_func, f"   ✅ Đã copy session JSON ({len(body_text)} chars)")
                            return data, body_text

                time.sleep(0.2)

            body_text = _read_body_text(driver)
            last_body = body_text
            _log(log_func, f"   [Debug] Session body length: {len(body_text)} chars")
            if body_text:
                data = json.loads(body_text)
                if isinstance(data, dict):
                    _log(log_func, f"   ✅ Đã copy và parse toàn bộ trang session ({len(data)} keys)")
                    return data, body_text
                _log(log_func, "   ⚠️ Session response không phải JSON object")
            else:
                _log(log_func, "   ⚠️ Body session trống, có thể tài khoản chưa đăng nhập xong")

        except json.JSONDecodeError as e:
            _log(log_func, f"   ⚠️ Lỗi parse JSON session: {e}")
            _log(log_func, f"   [Debug] Body đầu trang: {(last_body or '')[:200]}")
        except Exception as e:
            _log(log_func, f"   ⚠️ Lỗi lấy ChatGPT session: {e}")

        if attempt < max_retries - 1:
            time.sleep(0.5)

    _log(log_func, "   ❌ Không lấy được ChatGPT session sau tất cả các lần thử")
    return None, last_body


def get_access_token_from_browser(driver, max_retries=3, log_func=None):
    """
    Lay accessToken tu endpoint chatgpt.com/api/auth/session bang Selenium.
    """
    data, _raw_body = get_session_json_from_browser(
        driver,
        max_retries=max_retries,
        log_func=log_func,
    )
    if not data:
        return None

    token = data.get("accessToken")
    if token and isinstance(token, str) and len(token) > 50:
        _log(log_func, f"   ✅ Đã lấy được accessToken ({len(token)} ký tự)")
        return token

    _log(log_func, "   ❌ accessToken không hợp lệ hoặc trống")
    _log(log_func, f"   [Debug] Keys trong session: {list(data.keys())}")
    return None


def petrix_get_checkout(access_token, plan="team", currency="IDR", payment="shortlink", log_func=None):
    """
    Goi PetrixBot API de lay checkout link.
    """
    currency_map = {
        "IDR": "Indonesia",
        "VND": "Vietnam",
        "USD": "United States",
        "SGD": "Singapore",
        "MYR": "Malaysia",
        "THB": "Thailand",
        "KRW": "South Korea",
        "JPY": "Japan",
        "EUR": "Germany",
        "GBP": "United Kingdom",
        "AUD": "Australia",
    }
    region = currency_map.get(currency.upper(), currency) if len(currency) <= 3 else currency

    payload = {
        "plan": plan,
        "payment": payment,
        "currency": region,
        "session": access_token,
    }

    _log(log_func, f"   🌐 Đang gọi PetrixBot API (plan={plan}, currency={region}, payment={payment})...")

    try:
        resp = requests.post(
            PETRIX_PAYMENT,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Origin": "https://ezweystock.petrix.id",
                "Referer": "https://ezweystock.petrix.id/gpt/",
            },
            timeout=DEFAULT_TIMEOUT,
        )

        if resp.status_code != 200:
            _log(log_func, f"   ❌ PetrixBot API trả lỗi HTTP {resp.status_code}")
            _log(log_func, f"   📄 Response: {resp.text[:500]}")
            return None

        data = resp.json()
        _log(log_func, f"   [Debug] PetrixBot response: {json.dumps(data, ensure_ascii=False)[:500]}")

        url = _extract_checkout_url(data)

        if url and isinstance(url, str) and url.startswith(("http://", "https://")):
            _log(log_func, "   ✅ Đã lấy được checkout link 0 IDR từ PetrixBot")
            _log(log_func, f"   🔗 {url}")
            return url

        _log(log_func, "   ❌ PetrixBot API không trả về URL hợp lệ")
        return None

    except requests.Timeout:
        _log(log_func, f"   ❌ PetrixBot API timeout ({DEFAULT_TIMEOUT}s)")
        return None
    except requests.ConnectionError:
        _log(log_func, "   ❌ Không kết nối được tới PetrixBot API")
        return None
    except Exception as e:
        _log(log_func, f"   ❌ Lỗi gọi PetrixBot API: {e}")
        return None


def _extract_checkout_url(value):
    """Tìm checkout URL ở mọi vị trí trong response Petrix."""
    if isinstance(value, str):
        if value.startswith(("http://", "https://")) and "chatgpt.com/checkout/" in value:
            return value
        match = CHECKOUT_URL_RE.search(value)
        return match.group(0) if match else None

    if isinstance(value, dict):
        for key in ("url", "checkout_url", "checkoutUrl", "link", "payment_url", "paymentUrl", "shortlink"):
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


def get_currencies(log_func=None):
    """Lay danh sach currencies ho tro tu PetrixBot API."""
    try:
        resp = requests.get(PETRIX_CURRENCY, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        _log(log_func, f"   ⚠️ Lỗi lấy danh sách currency: {e}")
    return []


def petrix_generate_checkout(driver, plan_choice="Business", currency="IDR", log_func=None, max_retries=4):
    """
    Tien ich gom: lay ChatGPT session -> accessToken -> Petrix checkout link.
    """
    plan_map = {
        "business": "team",
        "team": "team",
        "plus": "plus",
    }
    plan = plan_map.get(str(plan_choice).lower(), "team")

    checkout_url = None
    for attempt in range(max_retries):
        _log(log_func, f"   🔁 Lấy link pay lần {attempt + 1}/{max_retries}")
        token = get_access_token_from_browser(driver, max_retries=4, log_func=log_func)
        if not token:
            try:
                driver.get(CHATGPT_HOME_URL)
                time.sleep(1.5)
            except Exception:
                pass
            continue

        checkout_url = petrix_get_checkout(
            access_token=token,
            plan=plan,
            currency=currency,
            log_func=log_func,
        )
        if checkout_url:
            break

        if attempt < max_retries - 1:
            wait_time = 2 + attempt
            _log(log_func, f"   ⏳ Chưa có link pay, chờ {wait_time}s rồi thử lại...")
            try:
                driver.get(CHATGPT_HOME_URL)
            except Exception:
                pass
            time.sleep(wait_time)

    try:
        driver.get(CHATGPT_HOME_URL)
    except Exception:
        pass

    return checkout_url


def open_checkout_in_new_tab(driver, checkout_url, log_func=None):
    """Mở checkout URL trong tab mới của cùng profile/browser."""
    if not checkout_url:
        return False
    try:
        before = set(driver.window_handles)
    except Exception:
        before = set()
    try:
        driver.execute_script("window.open(arguments[0], '_blank');", checkout_url)
        time.sleep(0.8)
        try:
            after = [h for h in driver.window_handles if h not in before]
            if after:
                driver.switch_to.window(after[-1])
        except Exception:
            pass
        try:
            from browser import apply_zoom_after_tab_switch
            apply_zoom_after_tab_switch(driver, zoom_factor=1.0)
        except Exception:
            pass
        _log(log_func, "   ✅ Đã mở link checkout Petrix trong tab mới của profile")
        _log(log_func, f"   🔗 {checkout_url}")
        return True
    except Exception as e:
        _log(log_func, f"   ⚠️ Mở tab mới checkout Petrix lỗi: {e}")
        try:
            driver.get(checkout_url)
            _log(log_func, "   ✅ Fallback mở checkout Petrix trên tab hiện tại")
            _log(log_func, f"   🔗 {checkout_url}")
            return True
        except Exception as inner_e:
            _log(log_func, f"   ❌ Không mở được checkout Petrix: {inner_e}")
            return False
