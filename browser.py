"""
Module tự động hóa trình duyệt
Dùng undetected-chromedriver để thực hiện quy trình đăng ký ChatGPT
"""

import re
import atexit
import os
import random
import subprocess
import threading
import time
import base64
import hashlib
import hmac
import struct
import undetected_chromedriver as uc
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

from config import (
    MAX_WAIT_TIME,
    SHORT_WAIT_TIME,
    ERROR_PAGE_MAX_RETRIES,
    BUTTON_CLICK_MAX_RETRIES,
    BACKGROUND_MODE,
    VISIBLE_GRID_ENABLED,
    VISIBLE_GRID_COLS,
    VISIBLE_GRID_ROWS,
    VISIBLE_WINDOW_WIDTH,
    VISIBLE_WINDOW_HEIGHT,
    VISIBLE_START_X,
    VISIBLE_START_Y,
    CREDIT_CARD_INFO,
    USER_AGENT,
    GPM_ENABLED,
    GPM_API_URL,
    GPM_PROFILE_IDS,
    GPM_START_ENDPOINT,
    GPM_AUTO_CREATE,
    GPM_AUTO_PROFILE_PREFIX,
    GPM_GROUP_NAME,
    GPM_BROWSER_VERSION,
    GPM_OS_TYPE,
    GPM_OS,
    GPM_RAW_PROXY,
    GPM_DELETE_CREATED_ON_CLOSE,
    OFFSCREEN_X,
    OFFSCREEN_Y,
    PAYMENT_FLOW,
)
from utils import generate_user_info, generate_billing_info, http_session


_gpm_profile_lock = threading.Lock()
_gpm_profile_index = 0
_gpm_browser_binary_cache = None
_gpm_local_driver_cache = None
_active_gpm_profiles = {}
_active_gpm_profiles_lock = threading.Lock()
_visible_window_slot_lock = threading.Lock()
_visible_window_slot_index = 0
_visible_grid_override = None
_profile_zoom_override = None
HOME_READY_STABLE_SECONDS = 5
OTP_POST_SUBMIT_TRANSITION_TIMEOUT = 10


class BrowserStartupError(RuntimeError):
    pass


def set_visible_grid_override(cols=None, rows=None, width=None, height=None):
    """Override tạm thời layout visible-grid cho batch hiện tại."""
    global _visible_grid_override, _visible_window_slot_index
    with _visible_window_slot_lock:
        if cols and rows:
            _visible_grid_override = {
                "cols": max(1, int(cols)),
                "rows": max(1, int(rows)),
                "width": max(1, int(width)) if width else None,
                "height": max(1, int(height)) if height else None,
            }
        else:
            _visible_grid_override = None
        _visible_window_slot_index = 0


def set_profile_zoom_override(zoom_factor=None):
    global _profile_zoom_override
    _profile_zoom_override = float(zoom_factor) if zoom_factor else None


def get_profile_zoom_factor(default=1.0):
    return float(_profile_zoom_override) if _profile_zoom_override else float(default)


def get_visible_grid_layout():
    layout = _visible_grid_override or {
        "cols": max(1, int(VISIBLE_GRID_COLS or 2)),
        "rows": max(1, int(VISIBLE_GRID_ROWS or 2)),
        "width": int(VISIBLE_WINDOW_WIDTH or 720),
        "height": int(VISIBLE_WINDOW_HEIGHT or 450),
    }
    return (
        layout["cols"],
        layout["rows"],
        int(layout.get("width") or VISIBLE_WINDOW_WIDTH or 720),
        int(layout.get("height") or VISIBLE_WINDOW_HEIGHT or 450),
    )


def get_chrome_major_version():
    """Tự phát hiện major version của Google Chrome đang cài."""
    chrome_commands = [
        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--version"],
        ["google-chrome", "--version"],
        ["chromium", "--version"],
        ["chromium-browser", "--version"],
    ]

    for command in chrome_commands:
        try:
            output = subprocess.check_output(command, stderr=subprocess.STDOUT, text=True).strip()
        except Exception:
            continue

        match = re.search(r"(\d+)\.", output)
        if match:
            version = int(match.group(1))
            print(f"🌐 Phát hiện Chrome version: {output} -> dùng ChromeDriver major {version}")
            return version

    print("⚠️ Không tự phát hiện được Chrome version, để undetected_chromedriver tự chọn driver")
    return None


def _pick_gpm_profile_id():
    global _gpm_profile_index

    profile_ids = [str(profile_id).strip() for profile_id in GPM_PROFILE_IDS if str(profile_id).strip()]
    if not profile_ids and GPM_AUTO_CREATE:
        return _create_gpm_profile()
    if not profile_ids:
        profile_ids = _load_gpm_profile_ids()
    if not profile_ids:
        raise RuntimeError(
            "browser.gpm_enabled=true nhưng không tìm thấy profile GPM nào. "
            "Hãy tạo profile trong GPM Login hoặc cấu hình browser.gpm_profile_ids trong config.yaml"
        )

    with _gpm_profile_lock:
        profile_id = profile_ids[_gpm_profile_index % len(profile_ids)]
        _gpm_profile_index += 1
        return profile_id


def _create_gpm_profile():
    api_url = GPM_API_URL.rstrip("/")
    create_url = f"{api_url}/api/v1/profiles/create"
    profile_name = f"{GPM_AUTO_PROFILE_PREFIX}-{int(time.time())}-{random.randint(1000, 9999)}"
    browser_version = _resolve_gpm_browser_version()
    payload = {
        "name": profile_name,
        "group_id": None,
        "raw_proxy": GPM_RAW_PROXY or "",
        "browser_type": 1,
        "browser_version": browser_version,
        "os_type": GPM_OS_TYPE,
        "custom_user_agent": USER_AGENT or None,
        "task_bar_title": profile_name,
        "webrtc_mode": None,
        "fixed_webrtc_public_ip": "",
        "geolocation_mode": None,
        "canvas_mode": None,
        "client_rect_mode": None,
        "webgl_image_mode": None,
        "webgl_metadata_mode": None,
        "audio_mode": None,
        "font_mode": None,
        "timezone_base_on_ip": True,
        "timezone": None,
        "is_language_base_on_ip": True,
        "fixed_language": None,
    }

    print(f"🧬 Đang tự tạo GPM profile mới: {profile_name}")
    response = http_session.post(create_url, json=payload, timeout=45)
    response.raise_for_status()
    data = response.json()

    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(f"GPM tạo profile thất bại: {data}")

    profile_data = data.get("data", {}) if isinstance(data, dict) else {}
    if isinstance(profile_data, dict):
        profile_id = str(profile_data.get("id", "")).strip()
    elif isinstance(profile_data, str):
        profile_id = profile_data.strip()
    else:
        profile_id = ""
    if not profile_id:
        raise RuntimeError(f"GPM tạo profile nhưng không trả id: {data}")
    if profile_id == "GPMLogin Global API" or any(char.isspace() for char in profile_id):
        raise BrowserStartupError(
            "GPM API trả về không phải profile ID. "
            f"Response: {data}. Kiểm tra lại browser.gpm_api_url/base URL trong config.yaml"
        )

    print(f"✅ Đã tạo GPM profile: {profile_name} ({profile_id})")
    return profile_id


def _load_gpm_profile_ids():
    profiles = _fetch_gpm_profiles()
    profile_ids = [
        str(profile.get("id", "")).strip()
        for profile in profiles
        if isinstance(profile, dict) and str(profile.get("id", "")).strip()
    ]

    if profile_ids:
        print(f"✅ Đã lấy {len(profile_ids)} profile từ GPM Login")
    return profile_ids


def _fetch_gpm_profiles():
    api_url = GPM_API_URL.rstrip("/")
    profiles_url = f"{api_url}/api/v1/profiles"
    print(f"📋 Đang lấy danh sách profile từ {profiles_url}")

    response = http_session.get(
        profiles_url,
        params={"page": 1, "per_page": 100, "sort": 0},
        timeout=12,
    )
    response.raise_for_status()
    data = response.json()

    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(f"GPM lấy danh sách profile thất bại: {data}")

    response_data = data.get("data", {}) if isinstance(data, dict) else {}
    if isinstance(response_data, dict):
        profiles = response_data.get("data", [])
    elif isinstance(response_data, list):
        profiles = response_data
    else:
        profiles = []
    return profiles


def _resolve_gpm_browser_version():
    configured_version = str(GPM_BROWSER_VERSION or "").strip()
    if configured_version and configured_version.lower() != "auto":
        return configured_version

    for profile in _fetch_gpm_profiles():
        if not isinstance(profile, dict):
            continue
        profile_name = str(profile.get("name", "")).strip()
        if profile_name.startswith(GPM_AUTO_PROFILE_PREFIX):
            continue
        browser_info = profile.get("browser") or {}
        version = ""
        if isinstance(browser_info, dict):
            version = str(browser_info.get("version", "")).strip()
        if not version:
            version = str(profile.get("browser_version", "")).strip()
        if version:
            print(f"✅ Dùng GPM browser version có sẵn: {version}")
            return version

    fallback_version = "139.0.7258.139"
    print(
        "⚠️ Không tìm thấy profile thủ công để lấy browser version, "
        f"dùng version mẫu GPM Global docs: {fallback_version}"
    )
    return fallback_version


def _extract_gpm_driver_info(response_json):
    payload = response_json.get("data") if isinstance(response_json, dict) else None
    if not isinstance(payload, dict):
        payload = response_json if isinstance(response_json, dict) else {}

    debugger_address = (
        payload.get("remote_debugging_address")
        or payload.get("remoteDebuggingAddress")
        or payload.get("debugger_address")
        or payload.get("selenium_remote_debug_address")
    )
    if not debugger_address and payload.get("remote_debugging_port"):
        debugger_address = f"127.0.0.1:{payload.get('remote_debugging_port')}"
    driver_path = (
        payload.get("driver_path")
        or payload.get("driverPath")
        or payload.get("chromedriver")
        or payload.get("chromedriver_path")
    )
    return debugger_address, driver_path


def _find_gpm_browser_binary():
    global _gpm_browser_binary_cache
    if _gpm_browser_binary_cache and os.path.exists(_gpm_browser_binary_cache):
        return _gpm_browser_binary_cache

    version = str(GPM_BROWSER_VERSION or "").strip()
    major = version.split(".", 1)[0] if version and version.lower() != "auto" else ""
    candidates = []
    if major:
        candidates.append(
            os.path.expanduser(
                f"~/Library/Application Support/GPMLoginGlobal/Browsers/ChromiumCore_v{major}/chrome.app/Contents/MacOS/Google Chrome"
            )
        )
    candidates.extend([
        os.path.expanduser("~/Library/Application Support/GPMLoginGlobal/Browsers/ChromiumCore_v147/chrome.app/Contents/MacOS/Google Chrome"),
        os.path.expanduser("~/Library/Application Support/GPMLoginGlobal/Browsers/ChromiumCore_v144/chrome.app/Contents/MacOS/Google Chrome"),
        os.path.expanduser("~/Library/Application Support/GPMLoginGlobal/Browsers/ChromiumCore_v142/chrome.app/Contents/MacOS/Google Chrome"),
    ])

    for path in candidates:
        if path and os.path.exists(path):
            _gpm_browser_binary_cache = path
            return path
    return ""


def _find_local_chromedriver():
    global _gpm_local_driver_cache
    if _gpm_local_driver_cache and os.path.exists(_gpm_local_driver_cache) and os.access(_gpm_local_driver_cache, os.X_OK):
        return _gpm_local_driver_cache

    candidates = [
        os.path.expanduser("~/Library/Application Support/undetected_chromedriver/undetected_chromedriver"),
        "/opt/homebrew/bin/chromedriver",
        "/usr/local/bin/chromedriver",
    ]
    for path in candidates:
        if path and os.path.exists(path) and os.access(path, os.X_OK):
            _gpm_local_driver_cache = path
            return path
    return ""


def _attach_gpm_close_hook(driver, profile_id):
    original_quit = driver.quit

    def quit_with_gpm_close():
        _cleanup_gpm_profile(profile_id, reason="driver.quit", stop_first=True, delete_created=GPM_AUTO_CREATE)
        try:
            original_quit()
        finally:
            _cleanup_gpm_profile(profile_id, reason="sau driver.quit", stop_first=True, delete_created=GPM_AUTO_CREATE)

    driver.gpm_profile_id = profile_id
    driver.quit = quit_with_gpm_close


def _register_active_gpm_profile(profile_id, auto_created=True):
    with _active_gpm_profiles_lock:
        _active_gpm_profiles[str(profile_id)] = bool(auto_created)


def _unregister_active_gpm_profile(profile_id):
    with _active_gpm_profiles_lock:
        _active_gpm_profiles.pop(str(profile_id), None)


def _cleanup_gpm_profile(profile_id, reason="", stop_first=True, delete_created=True):
    if not profile_id:
        return
    if stop_first:
        _stop_gpm_profile(profile_id, reason=reason)
    should_delete = bool(delete_created and GPM_DELETE_CREATED_ON_CLOSE)
    if should_delete:
        _delete_gpm_profile(profile_id, reason=reason)
        _unregister_active_gpm_profile(profile_id)


def cleanup_active_gpm_profiles(reason="process thoát"):
    with _active_gpm_profiles_lock:
        profiles = list(_active_gpm_profiles.items())
    for profile_id, auto_created in profiles:
        _cleanup_gpm_profile(profile_id, reason=reason, stop_first=True, delete_created=auto_created)


atexit.register(cleanup_active_gpm_profiles)


def _delete_gpm_profile(profile_id, reason=""):
    try:
        delete_url = f"{GPM_API_URL.rstrip('/')}/api/v1/profiles/delete/{profile_id}"
        http_session.get(delete_url, params={"mode": "hard"}, timeout=15)
        suffix = f" ({reason})" if reason else ""
        print(f"🗑️ Đã xoá GPM profile tự tạo: {profile_id}{suffix}")
    except Exception as e:
        print(f"⚠️ Xoá GPM profile tự tạo thất bại: {e}")


def _stop_gpm_profile(profile_id, reason=""):
    try:
        stop_url = f"{GPM_API_URL.rstrip('/')}/api/v1/profiles/stop/{profile_id}"
        http_session.get(stop_url, timeout=15)
        suffix = f" ({reason})" if reason else ""
        print(f"✅ Đã stop GPM profile: {profile_id}{suffix}")
    except Exception as e:
        print(f"⚠️ Stop GPM profile thất bại: {e}")


def _move_browser_offscreen(driver):
    if VISIBLE_GRID_ENABLED:
        return
    if not BACKGROUND_MODE:
        return

    moved = False
    try:
        window = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
        window_id = window.get("windowId")
        if window_id:
            driver.execute_cdp_cmd("Browser.setWindowBounds", {
                "windowId": window_id,
                "bounds": {
                    "left": int(OFFSCREEN_X),
                    "top": int(OFFSCREEN_Y),
                    "width": 1200,
                    "height": 800,
                    "windowState": "normal",
                },
            })
            moved = True
    except Exception as e:
        print(f"⚠️ CDP không chuyển được cửa sổ ra ngoài màn hình: {e}")

    try:
        driver.set_window_position(OFFSCREEN_X, OFFSCREEN_Y)
        driver.set_window_size(1200, 800)
        moved = True
    except Exception as e:
        print(f"⚠️ Selenium không chuyển được cửa sổ ra ngoài màn hình: {e}")

    try:
        driver.minimize_window()
    except Exception:
        pass

    if moved:
        print(f"👻 Đã chuyển/minimize cửa sổ trình duyệt để chạy nền: {OFFSCREEN_X},{OFFSCREEN_Y}")


def _bring_browser_to_front_for_auth(driver):
    """Hiện cửa sổ tạm thời để click landing login bằng thao tác thật."""
    if VISIBLE_GRID_ENABLED:
        rect = getattr(driver, "gpm_window_rect", None) or _allocate_visible_window_rect()
        _set_browser_window_rect(driver, rect, label="profile visible-grid")
        return
    if not BACKGROUND_MODE:
        return

    moved = False
    try:
        window = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
        window_id = window.get("windowId")
        if window_id:
            driver.execute_cdp_cmd("Browser.setWindowBounds", {
                "windowId": window_id,
                "bounds": {
                    "left": 80,
                    "top": 80,
                    "width": 1200,
                    "height": 850,
                    "windowState": "normal",
                },
            })
            moved = True
    except Exception as e:
        print(f"⚠️ CDP không đưa được cửa sổ ra trước: {e}")

    try:
        driver.set_window_position(80, 80)
        driver.set_window_size(1200, 850)
        driver.switch_to.window(driver.current_window_handle)
        driver.execute_script("window.focus();")
        moved = True
    except Exception as e:
        print(f"⚠️ Selenium không focus được cửa sổ auth: {e}")

    if moved:
        print("🪟 Tạm hiện/focus trình duyệt để click nút Đăng nhập đầu tiên")


def _allocate_visible_window_rect():
    global _visible_window_slot_index
    cols, rows, width, height = get_visible_grid_layout()
    total = max(1, cols * rows)
    with _visible_window_slot_lock:
        slot = _visible_window_slot_index % total
        _visible_window_slot_index += 1

    col = slot % cols
    row = slot // cols
    return {
        "left": int(VISIBLE_START_X) + col * width,
        "top": int(VISIBLE_START_Y) + row * height,
        "width": width,
        "height": height,
    }


def _set_browser_window_rect(driver, rect, label="profile"):
    moved = False
    try:
        window = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
        window_id = window.get("windowId")
        if window_id:
            driver.execute_cdp_cmd("Browser.setWindowBounds", {
                "windowId": window_id,
                "bounds": {
                    "left": int(rect["left"]),
                    "top": int(rect["top"]),
                    "width": int(rect["width"]),
                    "height": int(rect["height"]),
                    "windowState": "normal",
                },
            })
            moved = True
    except Exception as e:
        print(f"⚠️ CDP không đặt được vị trí {label}: {e}")

    try:
        driver.set_window_position(int(rect["left"]), int(rect["top"]))
        driver.set_window_size(int(rect["width"]), int(rect["height"]))
        driver.switch_to.window(driver.current_window_handle)
        driver.execute_script("window.focus();")
        moved = True
    except Exception as e:
        print(f"⚠️ Selenium không đặt/focus được {label}: {e}")

    if moved:
        print(
            f"🪟 Đặt {label}: x={rect['left']}, y={rect['top']}, "
            f"w={rect['width']}, h={rect['height']}"
        )


def create_gpm_driver():
    started_at = time.perf_counter()
    profile_id = _pick_gpm_profile_id()
    api_url = GPM_API_URL.rstrip("/")
    endpoint = GPM_START_ENDPOINT.format(profile_id=profile_id).lstrip("/")
    start_url = f"{api_url}/{endpoint}"

    print(f"🧬 Đang mở GPM Login profile: {profile_id}")
    start_params = {}
    visible_rect = _allocate_visible_window_rect() if VISIBLE_GRID_ENABLED else None
    if visible_rect:
        launch_args = (
            f"--window-position={visible_rect['left']},{visible_rect['top']} "
            f"--window-size={visible_rect['width']},{visible_rect['height']}"
        )
        start_params.update({
            "win_pos": f"{visible_rect['left']},{visible_rect['top']}",
            "win_size": f"{visible_rect['width']},{visible_rect['height']}",
            "window_position": f"{visible_rect['left']},{visible_rect['top']}",
            "window_size": f"{visible_rect['width']},{visible_rect['height']}",
            "args": launch_args,
            "addination_args": launch_args,
            "additional_args": launch_args,
            "browser_args": launch_args,
        })
        print(
            "🪟 GPM visible_grid=true, mở profile tại "
            f"{visible_rect['left']},{visible_rect['top']} "
            f"{visible_rect['width']}x{visible_rect['height']}"
        )
    elif BACKGROUND_MODE:
        launch_args = f"--window-position={OFFSCREEN_X},{OFFSCREEN_Y} --window-size=1200,800"
        start_params.update({
            "win_pos": f"{OFFSCREEN_X},{OFFSCREEN_Y}",
            "win_size": "1200,800",
            "window_position": f"{OFFSCREEN_X},{OFFSCREEN_Y}",
            "window_size": "1200,800",
            "args": launch_args,
            "addination_args": launch_args,
            "additional_args": launch_args,
            "browser_args": launch_args,
        })
        print(f"👻 GPM background_mode=true, yêu cầu mở profile ngoài màn hình: {OFFSCREEN_X},{OFFSCREEN_Y}")

    response = http_session.get(start_url, params=start_params or None, timeout=45)
    response.raise_for_status()
    data = response.json()

    if isinstance(data, dict) and data.get("success") is False:
        if GPM_AUTO_CREATE:
            _delete_gpm_profile(profile_id, reason="start thất bại")
        raise BrowserStartupError(f"GPM start profile thất bại: {data}")

    _register_active_gpm_profile(profile_id, auto_created=GPM_AUTO_CREATE)

    debugger_address, driver_path = _extract_gpm_driver_info(data)
    if not debugger_address:
        _cleanup_gpm_profile(profile_id, reason="không có debugger address", stop_first=True, delete_created=GPM_AUTO_CREATE)
        raise BrowserStartupError(f"GPM không trả remote_debugging_port/remote_debugging_address: {data}")

    options = webdriver.ChromeOptions()
    options.add_experimental_option("debuggerAddress", debugger_address)
    browser_binary = _find_gpm_browser_binary()
    if browser_binary:
        options.binary_location = browser_binary
        print(f"🧭 Dùng GPM browser binary: {browser_binary}")

    print(f"🔌 Attach Selenium vào GPM browser: {debugger_address}")
    if driver_path and os.path.exists(driver_path) and os.access(driver_path, os.X_OK):
        print(f"🧭 Dùng GPM driver_path: {driver_path}")
        service = ChromeService(executable_path=driver_path)
    else:
        if driver_path:
            print(f"⚠️ GPM trả driver_path nhưng không dùng được: {driver_path}")
        local_driver = _find_local_chromedriver()
        if local_driver:
            print(f"🧭 GPM không trả driver_path, dùng local ChromeDriver: {local_driver}")
            service = ChromeService(executable_path=local_driver)
        else:
            print("⚠️ GPM không trả driver_path, Selenium sẽ tự tìm ChromeDriver trong PATH")
            service = ChromeService()

    try:
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as e:
        _cleanup_gpm_profile(profile_id, reason="attach Selenium thất bại", stop_first=True, delete_created=GPM_AUTO_CREATE)
        raise BrowserStartupError(f"Attach Selenium vào GPM browser thất bại: {e}") from e

    _attach_gpm_close_hook(driver, profile_id)
    if visible_rect:
        driver.gpm_window_rect = visible_rect
        _set_browser_window_rect(driver, visible_rect, label="profile visible-grid")
    else:
        _move_browser_offscreen(driver)
    apply_default_profile_zoom(driver, zoom_factor=1.0)
    driver.set_page_load_timeout(120)
    driver.set_script_timeout(30)
    print(f"✅ Selenium đã attach vào GPM Login profile ({time.perf_counter() - started_at:.2f}s)")
    return driver


class SafeChrome(uc.Chrome):
    """
    Lớp Chrome tùy chỉnh, sửa lỗi WinError 6 khi thoát trên Windows
    """
    def __del__(self):
        try:
            self.quit()
        except OSError:
            pass
        except Exception:
            pass

    def quit(self):
        try:
            super().quit()
        except OSError:
            pass
        except Exception:
            pass


def create_driver(headless=False):
    """
    Tạo driver trình duyệt undetected Chrome
    
    Tham số:
        headless (bool): có dùng chế độ headless hay không
        
    Trả về:
        uc.Chrome: instance driver trình duyệt
    """
    headless = bool(headless or BACKGROUND_MODE)

    if GPM_ENABLED:
        print("🧬 browser.gpm_enabled=true, dùng trình duyệt anti-detect từ GPM Login")
        return create_gpm_driver()

    print(f"🌐 Đang khởi tạo trình duyệt (Headless: {headless})...")
    options = uc.ChromeOptions()
    
    # === Chế độ headless giả (Fake Headless) ===
    # Headless thật rất khó vượt Cloudflare, nên dùng chiến lược đưa cửa sổ ra ngoài màn hình
    # Cách này vẫn giữ fingerprint trình duyệt đầy đủ nhưng người dùng không thấy cửa sổ
    real_headless = False
    
    if headless:
        print("  👻 Dùng chế độ headless giả (off-screen) để vượt kiểm tra...")
        options.add_argument(f"--window-position={OFFSCREEN_X},{OFFSCREEN_Y}")
        options.add_argument("--window-size=1200,800")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        
        # Vẫn có thể thêm vài lớp ngụy trang dù không bắt buộc vì đây đã là trình duyệt thật
        options.add_argument("--lang=zh-CN,zh;q=0.9,en;q=0.8")
    
    # Dùng SafeChrome tùy chỉnh, truyền version_main để tránh lệch ChromeDriver/Chrome.
    chrome_major_version = get_chrome_major_version()
    driver_kwargs = {
        "options": options,
        "use_subprocess": True,
        "headless": real_headless,
    }
    if chrome_major_version:
        driver_kwargs["version_main"] = chrome_major_version

    print("🌐 Đang tạo ChromeDriver session...")
    driver = SafeChrome(**driver_kwargs)
    driver.set_page_load_timeout(120)
    driver.set_script_timeout(30)
    print("✅ ChromeDriver session đã sẵn sàng")
    apply_default_profile_zoom(driver, zoom_factor=1.0)
    
    # === Ngụy trang sâu cho chế độ headless ===
    if headless:
        print("🎭 Áp dụng ngụy trang fingerprint sâu...")
        
        # 1. Giả lập vendor WebGL để trông như có GPU thật
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    // 37445: UNMASKED_VENDOR_WEBGL
                    // 37446: UNMASKED_RENDERER_WEBGL
                    if (parameter === 37445) {
                        return 'Intel Inc.';
                    }
                    if (parameter === 37446) {
                        return 'Intel(R) Iris(R) Xe Graphics';
                    }
                    return getParameter(parameter);
                };
            """
        })
        
        # 2. Giả lập danh sách plugin vì headless mặc định rỗng
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                });
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['zh-CN', 'zh', 'en'],
                });
            """
        })
        
        # 3. Vượt các thuộc tính kiểm tra phổ biến
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                // Ghi đè window.chrome
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };
                
                // Giả lập permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                    Promise.resolve({ state: 'denied' }) :
                    originalQuery(parameters)
                );
            """
        })

    return driver


def check_and_handle_error(driver, max_retries=None):
    """
    Phát hiện lỗi trang và tự retry
    
    Tham số:
        driver: driver trình duyệt
        max_retries: số lần retry tối đa
    
    Trả về:
        bool: có phát hiện và xử lý lỗi hay không
    """
    if max_retries is None:
        max_retries = min(ERROR_PAGE_MAX_RETRIES, 2)

    try:
        retry_buttons = driver.find_elements(
            By.CSS_SELECTOR,
            'button[data-dd-action-name="Try again"], button[data-testid*="retry"], button[aria-label*="Try again"]'
        )
        for retry_btn in retry_buttons:
            if retry_btn.is_displayed() and retry_btn.is_enabled():
                print("⚠️ Phát hiện nút retry lỗi trang, click thử lại nhanh...")
                driver.execute_script("arguments[0].click();", retry_btn)
                time.sleep(0.8)
                return True

        visible_alerts = [
            el.text.strip().lower()
            for el in driver.find_elements(By.CSS_SELECTOR, '[role="alert"], .error-message')
            if el.is_displayed() and el.text.strip()
        ]
        hard_error_keywords = (
            "something went wrong",
            "try again",
            "timed out",
            "operation timeout",
            "route error",
            "invalid content",
        )
        if any(any(keyword in text for keyword in hard_error_keywords) for text in visible_alerts):
            print("⚠️ Phát hiện lỗi hiển thị trên trang, chờ ngắn rồi kiểm tra lại...")
            time.sleep(0.8)
            return True

        return False
    except Exception as e:
        print(f"  Ngoại lệ khi kiểm tra lỗi: {str(e).splitlines()[0]}")
        return False


def click_button_with_retry(driver, selector, max_retries=None):
    """
    Click nút có cơ chế retry
    
    Tham số:
        driver: driver trình duyệt
        selector: CSS selector
        max_retries: số lần retry tối đa
    
    Trả về:
        bool: có click thành công hay không
    """
    if max_retries is None:
        max_retries = BUTTON_CLICK_MAX_RETRIES
    
    for attempt in range(max_retries):
        try:
            button = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            scroll_element_and_ancestors_into_view(driver, button)
            try:
                button.click()
            except Exception:
                driver.execute_script("arguments[0].click();", button)
            return True
        except Exception as e:
            print(f"  Lần {attempt + 1} lần click thất bại, đang retry...")
            time.sleep(0.5)
    
    return False


def prepare_registration_form_for_submit(driver):
    """Bắn event và blur các field để UI bật nút submit ổn định hơn."""
    try:
        driver.execute_script(
            """
            const fields = [...document.querySelectorAll('input, textarea, select')];
            for (const el of fields) {
                try {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                } catch (e) {}
            }
            try {
                if (document.activeElement && document.activeElement.blur) {
                    document.activeElement.blur();
                }
            } catch (e) {}
            try {
                document.body && document.body.click && document.body.click();
            } catch (e) {}
            """
        )
    except Exception:
        pass


def find_registration_continue_button(driver):
    """Tìm đúng nút Tiếp tục/Continue ở form hồ sơ, tránh nhầm nút phụ."""
    candidates = []
    selectors = [
        (By.CSS_SELECTOR, 'button[type="submit"]'),
        (By.CSS_SELECTOR, 'button[data-testid="continue-button"]'),
        (By.XPATH, '//button[contains(normalize-space(.), "Tiếp tục")]'),
        (By.XPATH, '//button[contains(normalize-space(.), "Continue")]'),
        (By.XPATH, '//button[contains(normalize-space(.), "Tiếp theo")]'),
        (By.XPATH, '//button[contains(normalize-space(.), "Next")]'),
    ]
    for by, value in selectors:
        try:
            candidates.extend(driver.find_elements(by, value))
        except Exception:
            continue

    best = None
    best_score = None
    for btn in candidates:
        try:
            if not btn.is_displayed():
                continue
            text = (btn.text or btn.get_attribute("aria-label") or "").strip().lower()
            if any(skip in text for skip in ("dùng email khác", "use another email", "gửi lại email", "resend")):
                continue
            rect = btn.rect or {}
            score = (
                0 if "tiếp tục" in text or "continue" in text else 1,
                -(rect.get("y", 0) or 0),
                -(rect.get("width", 0) or 0),
            )
            if best is None or score < best_score:
                best = btn
                best_score = score
        except Exception:
            continue
    return best


def submit_registration_continue(driver, timeout=10):
    """Submit form hồ sơ theo cách chắc hơn click thuần."""
    deadline = time.time() + timeout
    last_button = None

    while time.time() < deadline:
        prepare_registration_form_for_submit(driver)
        accept_profile_agreements_if_present(driver)
        button = find_registration_continue_button(driver)
        if button:
            last_button = button
            scroll_element_and_ancestors_into_view(driver, button)
            try:
                if button.is_enabled() and str(button.get_attribute("disabled") or "").lower() not in ("true", "disabled"):
                    try:
                        button.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", button)
                    return True
            except Exception:
                pass

            try:
                submitted = driver.execute_script(
                    """
                    const btn = arguments[0];
                    if (!btn) return false;
                    const form = btn.closest('form');
                    if (form && form.requestSubmit) {
                        form.requestSubmit(btn);
                        return true;
                    }
                    return false;
                    """,
                    button,
                )
                if submitted:
                    return True
            except Exception:
                pass

        time.sleep(0.35)

    if last_button:
        try:
            driver.execute_script("arguments[0].click();", last_button)
            return True
        except Exception:
            pass

    return False


def scroll_element_and_ancestors_into_view(driver, element):
    """Cuộn toàn bộ chain scrollable để phần tử và nút submit thật sự lộ ra."""
    try:
        driver.execute_script(
            """
            const el = arguments[0];
            if (!el) return;
            const doc = document.scrollingElement || document.documentElement || document.body;
            try {
                el.scrollIntoView({block: 'center', inline: 'center'});
            } catch (e) {}

            let node = el;
            while (node) {
                try {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    const canScrollY =
                        (style.overflowY === 'auto' || style.overflowY === 'scroll' || style.overflow === 'auto' || style.overflow === 'scroll') &&
                        node.scrollHeight > node.clientHeight + 8;
                    if (canScrollY) {
                        const nodeRect = node.getBoundingClientRect();
                        const targetTop = node.scrollTop + (rect.top - nodeRect.top) - Math.max(24, (node.clientHeight / 2) - (rect.height / 2));
                        node.scrollTop = Math.max(0, targetTop);
                    }
                } catch (e) {}
                node = node.parentElement;
            }

            try {
                const rect = el.getBoundingClientRect();
                const targetTop = (window.pageYOffset || doc.scrollTop || 0) + rect.top - Math.max(40, (window.innerHeight / 2) - (rect.height / 2));
                window.scrollTo(0, Math.max(0, targetTop));
            } catch (e) {}
        """,
            element,
        )
        time.sleep(0.12)
    except Exception:
        pass


def apply_default_profile_zoom(driver, zoom_factor=1.0):
    """Đặt mức thu phóng mặc định thấp để các form dài dễ thao tác hơn."""
    zoom_factor = get_profile_zoom_factor(zoom_factor)
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": f"""
                    (() => {{
                        const APPLY_ZOOM = () => {{
                            try {{
                                document.documentElement.style.zoom = "{zoom_factor}";
                                if (document.body) {{
                                    document.body.style.zoom = "{zoom_factor}";
                                }}
                            }} catch (e) {{}}
                        }};
                        APPLY_ZOOM();
                        document.addEventListener('DOMContentLoaded', APPLY_ZOOM, {{ once: false }});
                        window.addEventListener('load', APPLY_ZOOM, {{ once: false }});
                    }})();
                """
            },
        )
    except Exception:
        pass


def apply_zoom_after_tab_switch(driver, zoom_factor=1.0):
    """Re-apply zoom sau khi switch sang tab mới của cùng profile."""
    zoom_factor = get_profile_zoom_factor(zoom_factor)
    try:
        apply_default_profile_zoom(driver, zoom_factor=zoom_factor)
        time.sleep(0.15)
        driver.execute_script(
            """
            const zoom = arguments[0];
            try {
                document.documentElement.style.zoom = String(zoom);
                if (document.body) document.body.style.zoom = String(zoom);
            } catch (e) {}
            """,
            zoom_factor,
        )
    except Exception:
        pass


def set_registered_profile_name(driver, profile_name):
    try:
        setattr(driver, "registered_profile_name", str(profile_name or "").strip())
    except Exception:
        pass


def get_registered_profile_name(driver):
    try:
        return str(getattr(driver, "registered_profile_name", "") or "").strip()
    except Exception:
        return ""

    try:
        driver.execute_script(
            """
            const zoom = arguments[0];
            try {
                document.documentElement.style.zoom = String(zoom);
                if (document.body) document.body.style.zoom = String(zoom);
            } catch (e) {}
            """,
            zoom_factor,
        )
        print(f"🔎 Đặt thu phóng mặc định profile: {int(zoom_factor * 100)}%")
    except Exception:
        pass


def double_click_auth_button(driver, button):
    try:
        ActionChains(driver).move_to_element(button).double_click(button).perform()
        time.sleep(0.12)
        return
    except Exception:
        pass

    try:
        driver.execute_script(
            """
            const el = arguments[0];
            el.scrollIntoView({block: 'center', inline: 'center'});
            """,
            button,
        )
        ActionChains(driver).move_by_offset(0, 0).move_to_element(button).pause(0.03).click().pause(0.05).click().perform()
        time.sleep(0.12)
        return
    except Exception:
        pass

    driver.execute_script(
        """
        const el = arguments[0];
        el.scrollIntoView({block: 'center'});
        const r = el.getBoundingClientRect();
        const base = {
            bubbles: true,
            cancelable: true,
            view: window,
            clientX: r.left + r.width / 2,
            clientY: r.top + r.height / 2,
            button: 0,
        };
        for (const detail of [1, 2]) {
            const opts = {...base, detail};
            el.dispatchEvent(new PointerEvent('pointerdown', opts));
            el.dispatchEvent(new MouseEvent('mousedown', opts));
            el.dispatchEvent(new PointerEvent('pointerup', opts));
            el.dispatchEvent(new MouseEvent('mouseup', opts));
            el.dispatchEvent(new MouseEvent('click', opts));
        }
        el.dispatchEvent(new MouseEvent('dblclick', {...base, detail: 2}));
        """,
        button,
    )
    time.sleep(0.12)


def double_click_until_auth_page_changes(
    driver,
    find_button,
    is_ready,
    label="đăng nhập",
    timeout=20,
    interval=0.25,
    max_clicks=None,
):
    start_url = driver.current_url
    deadline = time.time() + timeout
    click_count = 0
    last_url = start_url

    while time.time() < deadline:
        if is_ready():
            return True
        try:
            current_url = driver.current_url
        except Exception:
            current_url = ""
        if current_url != last_url:
            print(f"  URL sau click {label}: {current_url}")
            last_url = current_url

        button = find_button()
        if not button:
            time.sleep(interval)
            continue

        try:
            double_click_auth_button(driver, button)
            click_count += 1
            print(f"  ✅ Đã double-click nút {label} ({click_count})")
        except Exception as e:
            if "stale element reference" in str(e).lower():
                time.sleep(0.1)
                continue
            print(f"  ⚠️ Không double-click được nút {label}: {str(e).splitlines()[0]}")

        settle_until = time.time() + 1.2
        while time.time() < settle_until:
            if is_ready():
                return True
            time.sleep(0.12)

        if max_clicks is not None and click_count >= max_clicks:
            print(f"  ⚠️ Đã thử {click_count} lần với nút {label} nhưng trang chưa chuyển")
            break

        time.sleep(interval)

    return is_ready()


def type_slowly(element, text, delay=0.05):
    """
    Mô phỏng nhập chậm như người thật
    
    Tham số:
        element: phần tử input
        text: văn bản cần nhập
        delay: độ trễ giữa mỗi ký tự(giây)
    """
    for char in text:
        element.send_keys(char)
        time.sleep(delay)


def fill_text_fast(driver, element, value):
    """Điền input nhanh nhưng vẫn bắn event để React nhận giá trị."""
    driver.execute_script(
        """
        const el = arguments[0];
        const value = arguments[1];
        const setter = Object.getOwnPropertyDescriptor(el.__proto__, 'value')?.set;
        if (setter) {
            setter.call(el, value);
        } else {
            el.value = value;
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        """,
        element,
        value,
    )


CODE_INPUT_SELECTOR = (
    'input[name="code"], '
    'input[autocomplete="one-time-code"], '
    'input[inputmode="numeric"], '
    'input[id*="code"], '
    'input[placeholder*="Code"], '
    'input[aria-label*="Code"], '
    'input[placeholder*="Mã"], '
    'input[aria-label*="Mã"]'
)
PROFILE_INPUT_SELECTOR = (
    'input[name="name"], '
    'input[autocomplete="name"], '
    'input[id*="name" i], '
    'input[placeholder*="name" i], '
    'input[aria-label*="name" i], '
    'input[placeholder*="tên" i], '
    'input[aria-label*="tên" i]'
)
AGE_INPUT_SELECTOR = (
    'input[name="age"], '
    'input[id*="age" i], '
    'input[placeholder*="age" i], '
    'input[aria-label*="age" i], '
    'input[placeholder*="tuổi" i], '
    'input[aria-label*="tuổi" i]'
)
BIRTHDATE_INPUT_SELECTOR = (
    'input[name*="birth" i], '
    'input[id*="birth" i], '
    'input[name*="birthday" i], '
    'input[id*="birthday" i], '
    'input[name*="bday" i], '
    'input[id*="bday" i], '
    'input[placeholder*="dd/mm/yyyy" i], '
    'input[placeholder*="dd / mm / yyyy" i], '
    'input[placeholder*="ngày sinh" i], '
    'input[aria-label*="date of birth" i], '
    'input[aria-label*="birthday" i], '
    'input[aria-label*="birth date" i], '
    'input[aria-label*="ngày sinh" i]'
)

CHATGPT_HOME_READY_SELECTOR = (
    '#prompt-textarea, '
    'textarea[data-testid="prompt-textarea"], '
    '[data-testid="composer"], '
    'main textarea, '
    'a[href="/"], '
    'nav a[href*="/"], '
    'button[aria-label*="new chat" i], '
    'button[aria-label*="đoạn chat mới" i], '
    'button[data-testid*="profile"], '
    'button[aria-label*="profile" i], '
    'button[aria-label*="account" i]'
)


def is_chatgpt_home_ready(driver):
    try:
        current_url = driver.current_url
    except Exception:
        current_url = ""

    if (
        current_url.startswith("https://chatgpt.com/")
        and "/auth/" not in current_url
        and "email-verification" not in current_url
        and "/about-you" not in current_url
    ):
        try:
            return bool(driver.find_elements(By.CSS_SELECTOR, CHATGPT_HOME_READY_SELECTOR))
        except Exception:
            return False

    return False


def _has_chatgpt_home_blocker_text(driver):
    try:
        text = (driver.execute_script("return document.body.innerText || ''") or "").lower()
    except Exception:
        return False
    blocker_keywords = (
        "điều gì thôi thúc bạn sử dụng chatgpt",
        "what brings you to chatgpt",
        "bạn đã hoàn tất",
        "you are all set",
        "lời khuyên để bắt đầu",
        "tips for getting started",
        "ok, tiến hành thôi",
    )
    return any(keyword in text for keyword in blocker_keywords)


def find_visible_element(driver, selector, timeout=30):
    """Tìm element hiển thị trong document chính với vòng chờ tự kiểm soát."""
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if el.is_displayed() and el.is_enabled():
                        return el
                except Exception:
                    continue
        except Exception as e:
            last_error = e

        check_and_handle_error(driver)

        time.sleep(0.2)

    if last_error:
        print(f"  [Debug] Lỗi cuối khi tìm selector {selector}: {last_error}")
    return None


def dismiss_cookie_banner(driver, timeout=3):
    """Đóng popup cookie trước khi click Login/Signup để tránh overlay che nút."""
    deadline = time.time() + timeout
    clicked = False
    cookie_button_xpaths = [
        '//button[contains(normalize-space(.), "Từ chối cookie không thiết yếu")]',
        '//button[contains(normalize-space(.), "Reject non-essential")]',
        '//button[contains(normalize-space(.), "Reject all")]',
        '//button[contains(normalize-space(.), "Từ chối")]',
        '//button[contains(normalize-space(.), "Chấp nhận tất cả")]',
        '//button[contains(normalize-space(.), "Accept all")]',
    ]
    close_xpaths = [
        '//*[contains(normalize-space(.), "Chúng tôi sử dụng cookie")]/ancestor::*[@role="dialog"]//button[@aria-label="Close" or @aria-label="Đóng"]',
        '//*[contains(normalize-space(.), "We use cookies")]/ancestor::*[@role="dialog"]//button[@aria-label="Close" or @aria-label="Đóng"]',
        '//*[@role="dialog" and (contains(., "cookie") or contains(., "Cookie"))]//button[not(normalize-space(.))]',
    ]

    while time.time() < deadline:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

        for xpath in cookie_button_xpaths + close_xpaths:
            try:
                buttons = driver.find_elements(By.XPATH, xpath)
            except Exception:
                buttons = []
            for btn in buttons:
                try:
                    if not btn.is_displayed() or not btn.is_enabled():
                        continue
                    text = (btn.text or btn.get_attribute("aria-label") or "nút đóng").strip()
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    try:
                        btn.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", btn)
                    print(f"  🍪 Đã xử lý popup cookie: {text}")
                    time.sleep(0.5)
                    clicked = True
                    return True
                except Exception:
                    continue

        if clicked:
            return True
        time.sleep(0.2)

    return False


def _document_ready_state(driver):
    try:
        return driver.execute_script("return document.readyState") or ""
    except Exception:
        return ""


def _wait_for_url_or_dom_settle(driver, previous_url="", timeout=12, stable_for=1.0):
    """Chờ navigation/ajax sau click lắng xuống trước khi đọc trạng thái tiếp theo."""
    deadline = time.time() + timeout
    last_signature = None
    stable_since = time.time()

    while time.time() < deadline:
        try:
            current_url = driver.current_url
        except Exception:
            current_url = ""

        ready_state = _document_ready_state(driver)
        try:
            input_count = len(driver.find_elements(By.CSS_SELECTOR, "input"))
        except Exception:
            input_count = -1

        signature = (current_url, ready_state, input_count)
        if signature != last_signature:
            last_signature = signature
            stable_since = time.time()
        elif ready_state == "complete" and time.time() - stable_since >= stable_for:
            return current_url

        if previous_url and current_url != previous_url and ready_state in ("interactive", "complete"):
            if time.time() - stable_since >= min(stable_for, 0.5):
                return current_url

        time.sleep(0.15)

    try:
        return driver.current_url
    except Exception:
        return ""


def classify_after_email_continue(driver):
    """Phân loại trạng thái sau khi submit email."""
    try:
        current_url = driver.current_url
    except Exception:
        current_url = ""

    lowered_url = (current_url or "").lower()
    if "chatgpt.com/auth/error" in lowered_url and "oauthcallback" in lowered_url:
        return "auth_oauth_error", current_url

    if _find_continue_with_password_candidates(driver):
        return "password_switch", ""

    if "email-verification/register" in current_url:
        code_input = find_code_input_fast(driver, timeout=0.2)
        inline_form_detected = bool(
            code_input or find_profile_name_input_fast(driver, timeout=0.2) or find_age_input(driver)
        )
        if inline_form_detected:
            return "inline_otp", ""

    if _visible_elements(driver, CODE_INPUT_SELECTOR):
        return "otp", ""

    if _visible_elements(driver, 'input[autocomplete="new-password"], input[name="password"], input[type="password"]'):
        return "password", ""

    email_inputs = _visible_elements(driver, 'input[type="email"], input[name="email"], input[autocomplete="email"]')
    if email_inputs:
        alerts = _visible_alert_texts(driver)
        return "email_still_visible", alerts[0] if alerts else ""

    alerts = _visible_alert_texts(driver)
    if alerts:
        return "page_error", alerts[0]

    if is_chatgpt_home_ready(driver):
        return "home", ""

    try:
        auth_entry_buttons = driver.find_elements(
            By.XPATH,
            '//*[self::a or self::button or @role="button"]'
            '[contains(normalize-space(.), "Đăng ký") '
            'or contains(normalize-space(.), "Sign up") '
            'or contains(normalize-space(.), "Đăng nhập") '
            'or contains(normalize-space(.), "Log in") '
            'or contains(normalize-space(.), "Login")]'
        )
        if any(btn.is_displayed() for btn in auth_entry_buttons):
            return "auth_entry", current_url
    except Exception:
        pass

    ready_state = _document_ready_state(driver)
    if ready_state != "complete":
        return "loading", ready_state

    return "unknown", current_url


def _find_continue_with_password_candidates(driver):
    selectors = [
        (By.XPATH, '//button[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZÀÁẠẢÃĂẮẰẲẴẶÂẤẦẨẪẬĐÈÉẸẺẼÊẾỀỂỄỆÌÍỊỈĨÒÓỌỎÕÔỐỒỔỖỘƠỚỜỞỠỢÙÚỤỦŨƯỨỪỬỮỰỲÝỴỶỸ", "abcdefghijklmnopqrstuvwxyzàáạảãăắằẳẵặâấầẩẫậđèéẹẻẽêếềểễệìíịỉĩòóọỏõôốồổỗộơớờởỡợùúụủũưứừửữựỳýỵỷỹ"), "tiếp tục với mật khẩu")]'),
        (By.XPATH, '//button[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "continue with password")]'),
        (By.XPATH, '//button[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "password instead")]'),
        (By.XPATH, '//a[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZÀÁẠẢÃĂẮẰẲẴẶÂẤẦẨẪẬĐÈÉẸẺẼÊẾỀỂỄỆÌÍỊỈĨÒÓỌỎÕÔỐỒỔỖỘƠỚỜỞỠỢÙÚỤỦŨƯỨỪỬỮỰỲÝỴỶỸ", "abcdefghijklmnopqrstuvwxyzàáạảãăắằẳẵặâấầẩẫậđèéẹẻẽêếềểễệìíịỉĩòóọỏõôốồổỗộơớờởỡợùúụủũưứừửữựỳýỵỷỹ"), "tiếp tục với mật khẩu")]'),
        (By.XPATH, '//a[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "continue with password")]'),
    ]
    candidates = []
    for by, selector in selectors:
        try:
            candidates.extend(driver.find_elements(by, selector))
        except Exception:
            continue

    visible = []
    for candidate in candidates:
        try:
            if candidate.is_displayed() and candidate.is_enabled():
                visible.append(candidate)
        except Exception:
            continue
    return visible


def click_continue_with_password(driver, timeout=8):
    """Ưu tiên chuyển về flow password trước khi điền OTP nếu UI cho phép."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        candidates = _find_continue_with_password_candidates(driver)
        if not candidates:
            time.sleep(0.25)
            continue

        for candidate in candidates:
            try:
                scroll_element_and_ancestors_into_view(driver, candidate)
                try:
                    candidate.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", candidate)
                print("✅ Đã bấm 'Tiếp tục với mật khẩu'")
                return True
            except Exception:
                continue
        time.sleep(0.25)

    print("⚠️ Không bấm được nút 'Tiếp tục với mật khẩu'")
    return False


def robust_fill_input(driver, element, value, label="input"):
    """Nhập text bằng thao tác thật, fallback sang JS nếu trang không nhận."""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    except Exception:
        pass

    try:
        actions = ActionChains(driver)
        actions.move_to_element(element).click().perform()
        time.sleep(0.05)
        element.send_keys(Keys.COMMAND, "a")
        element.send_keys(Keys.BACKSPACE)
        time.sleep(0.05)
        type_slowly(element, value, delay=0.015)
        time.sleep(0.1)
    except Exception as e:
        print(f"  ⚠️ Nhập {label} bằng phím thật lỗi, chuyển sang JS: {e}")

    try:
        current_value = element.get_attribute("value") or element.text or ""
    except Exception:
        current_value = ""

    if str(current_value).strip() != str(value).strip():
        fill_text_fast(driver, element, value)
        time.sleep(0.1)

    try:
        current_value = element.get_attribute("value") or element.text or ""
    except Exception:
        current_value = ""

    if str(value).strip() not in str(current_value).strip():
        print(f"  ⚠️ {label} có thể chưa được trang nhận, value hiện tại: {current_value}")
        return False

    return True


def fill_birthdate_ddmmyyyy_input(driver, element, value):
    """Nhập ô ngày sinh dạng DD/MM/YYYY theo kiểu thân thiện với input mask."""
    expected = str(value).strip()

    def read_current_value():
        try:
            return (element.get_attribute("value") or element.text or "").strip()
        except Exception:
            return ""

    def clear_birthdate_field():
        try:
            actions = ActionChains(driver)
            actions.move_to_element(element).click().perform()
            time.sleep(0.08)
        except Exception:
            pass

        # Thử xóa theo kiểu người dùng thật trước để input mask bỏ sạch giá trị mặc định.
        try:
            current_value = read_current_value()
            if current_value:
                element.send_keys(Keys.COMMAND, "a")
                time.sleep(0.05)
                element.send_keys(Keys.BACKSPACE)
                element.send_keys(Keys.DELETE)
                time.sleep(0.08)

                current_value = read_current_value()
                if current_value:
                    backspace_count = max(12, len(current_value) + 4)
                    element.send_keys(Keys.END)
                    time.sleep(0.03)
                    for _ in range(backspace_count):
                        element.send_keys(Keys.BACKSPACE)
                        element.send_keys(Keys.DELETE)
                        time.sleep(0.01)
                    time.sleep(0.08)
        except Exception:
            pass

        # Nếu mask vẫn giữ giá trị, ép rỗng bằng JS rồi bắn event.
        if read_current_value():
            try:
                driver.execute_script(
                    """
                    const el = arguments[0];
                    el.focus();
                    try {
                      if (typeof el.setSelectionRange === 'function') {
                        el.setSelectionRange(0, (el.value || '').length);
                      }
                    } catch (_err) {}
                    try {
                      if (typeof el.setRangeText === 'function') {
                        el.setRangeText('', 0, (el.value || '').length, 'end');
                      }
                    } catch (_err) {}
                    const setter = Object.getOwnPropertyDescriptor(el.__proto__, 'value')?.set;
                    if (setter) {
                        setter.call(el, '');
                    } else {
                        el.value = '';
                    }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    """,
                    element,
                )
                time.sleep(0.08)
            except Exception:
                pass

    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    except Exception:
        pass

    typed_ok = False
    try:
        actions = ActionChains(driver)
        actions.move_to_element(element).click().perform()
        time.sleep(0.08)
        clear_birthdate_field()
        time.sleep(0.05)
        try:
            element.send_keys(Keys.COMMAND, "a")
            time.sleep(0.03)
        except Exception:
            pass
        for char in expected:
            element.send_keys(char)
            time.sleep(0.03 if char == "/" else 0.02)
        time.sleep(0.15)
        typed_ok = True
    except Exception as e:
        print(f"  ⚠️ Nhập ngày sinh DD/MM/YYYY bằng phím thật lỗi, chuyển sang fallback: {e}")

    try:
        current_value = read_current_value()
    except Exception:
        current_value = ""

    if current_value == expected:
        return True

    try:
        driver.execute_script(
            """
            const el = arguments[0];
            const value = arguments[1];
            el.focus();
            const setter = Object.getOwnPropertyDescriptor(el.__proto__, 'value')?.set;
            if (setter) {
                setter.call(el, '');
                setter.call(el, value);
            } else {
                el.value = value;
            }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
            """,
            element,
            expected,
        )
        time.sleep(0.15)
    except Exception:
        pass

    try:
        current_value = read_current_value()
    except Exception:
        current_value = ""

    if current_value == expected:
        return True

    if typed_ok:
        print(f"  ⚠️ Ô ngày sinh chưa nhận đúng DD/MM/YYYY, value hiện tại: {current_value}")
    return False


def click_resend_email_button(driver, timeout=8):
    """Bấm nút Gửi lại email/Resend ở màn OTP nếu có."""
    selectors = [
        (By.XPATH, '//button[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "gửi lại email")]'),
        (By.XPATH, '//button[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "gui lai email")]'),
        (By.XPATH, '//button[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "resend")]'),
        (By.XPATH, '//button[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "send again")]'),
        (By.XPATH, '//a[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "gửi lại email")]'),
        (By.XPATH, '//a[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "resend")]'),
    ]
    deadline = time.time() + timeout
    while time.time() < deadline:
        for by, selector in selectors:
            try:
                buttons = driver.find_elements(by, selector)
            except Exception:
                buttons = []
            for button in buttons:
                try:
                    if not button.is_displayed() or not button.is_enabled():
                        continue
                    scroll_element_and_ancestors_into_view(driver, button)
                    try:
                        button.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", button)
                    print("📨 Đã bấm nút Gửi lại email / Resend")
                    return True
                except Exception:
                    continue
        time.sleep(0.3)
    print("⚠️ Không tìm thấy nút Gửi lại email / Resend để bấm")
    return False


def find_code_input_fast(driver, timeout=8):
    """Tìm ô OTP nhanh, gồm cả trường hợp trang dùng input rời/active element."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

        candidates = []
        try:
            candidates.extend(driver.find_elements(By.CSS_SELECTOR, CODE_INPUT_SELECTOR))
        except Exception:
            pass
        try:
            candidates.extend(driver.find_elements(By.CSS_SELECTOR, "input"))
        except Exception:
            pass

        seen = set()
        for el in candidates:
            try:
                if el.id in seen or not el.is_displayed() or not el.is_enabled():
                    continue
                seen.add(el.id)
                attrs = " ".join(
                    str(el.get_attribute(attr) or "")
                    for attr in ("name", "id", "placeholder", "aria-label", "autocomplete", "inputmode", "type")
                ).lower()
                if any(skip in attrs for skip in ("email", "password", "name", "age")):
                    continue
                if (
                    "code" in attrs
                    or "otp" in attrs
                    or "one-time" in attrs
                    or "mã" in attrs
                    or el.get_attribute("inputmode") == "numeric"
                    or el.get_attribute("type") in ("tel", "number", "text")
                ):
                    return el
            except Exception:
                continue

        try:
            active = driver.switch_to.active_element
            tag_name = (active.tag_name or "").lower()
            if tag_name in ("input", "textarea") and active.is_displayed() and active.is_enabled():
                return active
        except Exception:
            pass

        time.sleep(0.15)

    return None


def _visible_elements(driver, selector):
    try:
        return [
            el for el in driver.find_elements(By.CSS_SELECTOR, selector)
            if el.is_displayed()
        ]
    except Exception:
        return []


def _visible_alert_texts(driver):
    alerts = []
    for el in _visible_elements(driver, '[role="alert"], .error-message, [data-testid*="error"]'):
        try:
            text = el.text.strip()
        except Exception:
            text = ""
        if text:
            alerts.append(text)
    return alerts


def _is_invalid_otp_error(text):
    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in (
        "invalid code",
        "incorrect code",
        "wrong code",
        "expired code",
        "code is invalid",
        "mã không hợp lệ",
        "mã sai",
        "mã đã hết hạn",
    ))


def classify_after_otp_submit(driver):
    """Phân loại trạng thái sau khi submit OTP để tránh nhầm chuyển trang chậm là OTP sai."""
    try:
        current_url = driver.current_url
    except Exception:
        current_url = ""

    if is_chatgpt_home_ready(driver):
        return "home", ""

    if "/about-you" in current_url:
        return "about_you", ""

    if "email-verification/register" in current_url:
        if find_profile_name_input_fast(driver, timeout=0.2) or find_birthdate_input(driver) or find_age_input(driver):
            return "inline_profile", ""

    if _visible_elements(driver, PROFILE_INPUT_SELECTOR):
        return "profile_form", ""

    alerts = _visible_alert_texts(driver)
    for text in alerts:
        if _is_invalid_otp_error(text):
            return "otp_invalid", text
    if alerts:
        return "page_error", alerts[0]

    if _visible_elements(driver, CODE_INPUT_SELECTOR):
        return "otp_visible", ""

    return "transitioning", ""


def find_profile_name_input_fast(driver, timeout=10):
    """Tìm ô họ tên nhanh, fallback sang text input hợp lý trên about-you."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

        candidates = []
        try:
            candidates.extend(driver.find_elements(By.CSS_SELECTOR, PROFILE_INPUT_SELECTOR))
        except Exception:
            pass
        try:
            candidates.extend(driver.find_elements(By.CSS_SELECTOR, 'input[type="text"], input:not([type])'))
        except Exception:
            pass

        try:
            active = driver.switch_to.active_element
            if active and (active.tag_name or "").lower() == "input":
                candidates.insert(0, active)
        except Exception:
            pass

        seen = set()
        for el in candidates:
            try:
                if el.id in seen or not el.is_displayed() or not el.is_enabled():
                    continue
                seen.add(el.id)
                attrs = " ".join(
                    str(el.get_attribute(attr) or "")
                    for attr in ("name", "id", "placeholder", "aria-label", "autocomplete", "inputmode")
                ).lower()
                if any(skip in attrs for skip in ("email", "password", "code", "otp", "age", "tuổi", "bday", "year", "month", "day")):
                    continue
                return el
            except Exception:
                continue

        time.sleep(0.15)

    return None


def wait_for_chatgpt_home_ready(driver, timeout=120):
    """
    Chờ sau khi submit About you cho tới khi thật sự về trang chủ ChatGPT.
    Chỉ khi qua bước này mới nên gọi /api/auth/session.
    """
    print("⏳ Đang chờ ChatGPT load hẳn vào trang chủ sau About you...")
    deadline = time.time() + timeout
    last_url = ""
    home_ready_since = None
    while time.time() < deadline:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

        current_url = driver.current_url
        if current_url != last_url:
            print(f"  URL hiện tại: {current_url}")
            last_url = current_url

        check_and_handle_error(driver)

        lowered_url = (current_url or "").lower()
        url_ready = (
            lowered_url.startswith("https://chatgpt.com/")
            and "/auth/" not in lowered_url
            and "/about-you" not in lowered_url
            and "email-verification" not in lowered_url
        )

        home_ready = (
            url_ready
            or (is_chatgpt_home_ready(driver) and not _has_chatgpt_home_blocker_text(driver))
        )
        if home_ready:
            if home_ready_since is None:
                home_ready_since = time.time()
                print(f"✅ Đã thấy trang chủ ChatGPT, chờ ổn định {HOME_READY_STABLE_SECONDS}s...")
            elif time.time() - home_ready_since >= HOME_READY_STABLE_SECONDS:
                print("✅ Trang chủ ChatGPT đã ổn định, chuyển sang bước checkout mới")
                return True
        else:
            home_ready_since = None

        time.sleep(0.5)

    print("❌ Hết thời gian chờ ChatGPT trang chủ sẵn sàng")
    return False


def _normalize_2fa_secret(secret: str) -> str:
    return re.sub(r"[^A-Z2-7]", "", str(secret or "").upper())


def _generate_totp_code(secret: str, digits: int = 6, interval: int = 30) -> str:
    normalized = _normalize_2fa_secret(secret)
    key = base64.b32decode(normalized, casefold=True)
    counter = int(time.time() // interval)
    return _generate_totp_code_at_counter(key, counter, digits=digits)


def _generate_totp_code_at_counter(key: bytes, counter: int, digits: int = 6) -> str:
    counter_bytes = struct.pack(">Q", counter)
    digest = hmac.new(key, counter_bytes, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def _generate_totp_code_at(secret: str, timestamp: int, digits: int = 6, interval: int = 30) -> str:
    normalized = _normalize_2fa_secret(secret)
    key = base64.b32decode(normalized, casefold=True)
    counter = int(timestamp // interval)
    return _generate_totp_code_at_counter(key, counter, digits=digits)


def _extract_2fa_secret_from_page(driver):
    candidates = []
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text or ""
        candidates.append(body_text)
    except Exception:
        pass

    try:
        candidates.append(driver.page_source or "")
    except Exception:
        pass

    try:
        qr_nodes = driver.find_elements(By.CSS_SELECTOR, 'img, canvas, [data-testid*="qr"], [src*="otpauth"], [href*="otpauth"]')
        for node in qr_nodes:
            for attr in ("src", "href", "data-qr", "data-url"):
                try:
                    value = node.get_attribute(attr) or ""
                    if value:
                        candidates.append(value)
                except Exception:
                    continue
    except Exception:
        pass

    for candidate in candidates:
        if not candidate:
            continue
        otpauth_match = re.search(r"secret=([A-Z2-7]{16,64})", candidate, re.IGNORECASE)
        if otpauth_match:
            return _normalize_2fa_secret(otpauth_match.group(1))

        labeled_match = re.search(r"secret\s*key[^A-Z2-7]*([A-Z2-7][A-Z2-7\s-]{15,80})", candidate, re.IGNORECASE)
        if labeled_match:
            secret = _normalize_2fa_secret(labeled_match.group(1))
            if len(secret) >= 16:
                return secret

        generic_match = re.search(r"\b([A-Z2-7]{32})\b", candidate)
        if generic_match:
            return _normalize_2fa_secret(generic_match.group(1))

    return ""


def _collect_2fa_backup_codes(driver):
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        return []

    codes = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.fullmatch(r"[A-Z0-9-]{6,20}", line, re.IGNORECASE)
        if match and not line.lower().startswith(("chatgpt", "security", "backup")):
            codes.append(line)
    deduped = []
    for code in codes:
        if code not in deduped:
            deduped.append(code)
    return deduped[:20]


def _click_manual_2fa_secret_link(driver):
    selectors = [
        (By.XPATH, '//*[self::a or self::button or self::span][contains(normalize-space(.), "Bạn gặp vấn đề khi quét")]'),
        (By.XPATH, '//*[self::a or self::button or self::span][contains(normalize-space(.), "Trouble scanning")]'),
        (By.XPATH, '//*[self::a or self::button or self::span][contains(normalize-space(.), "Can\'t scan")]'),
    ]
    for by, selector in selectors:
        try:
            nodes = driver.find_elements(by, selector)
        except Exception:
            nodes = []
        for node in nodes:
            try:
                if not node.is_displayed():
                    continue
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", node)
                try:
                    node.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", node)
                time.sleep(0.35)
                return True
            except Exception:
                continue

    try:
        clicked = driver.execute_script(
            """
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const nodes = Array.from(document.querySelectorAll('a, button, span'));
            for (const node of nodes) {
                if (!node || node.offsetParent === null) continue;
                const text = norm(node.innerText || node.textContent || node.getAttribute('aria-label') || '');
                if (
                    text.includes('bạn gặp vấn đề khi quét') ||
                    text.includes('trouble scanning') ||
                    text.includes("can't scan")
                ) {
                    try {
                        node.scrollIntoView({ block: 'center', behavior: 'instant' });
                        node.click();
                        return true;
                    } catch (_err) {}
                }
            }
            return false;
            """
        )
        if clicked:
            time.sleep(0.35)
            return True
    except Exception:
        pass
    return False


def _click_2fa_verify_button(driver):
    try:
        clicked = driver.execute_script(
            """
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const dialogs = Array.from(document.querySelectorAll('[role="dialog"]')).filter((el) => el && el.offsetParent !== null);
            const root = dialogs.length ? dialogs[dialogs.length - 1] : document;
            const buttons = Array.from(root.querySelectorAll('button, [role="button"]')).filter((el) => el && el.offsetParent !== null);

            for (const button of buttons) {
                const text = norm(button.innerText || button.textContent || button.getAttribute('aria-label') || '');
                if (!text) continue;
                if (text.includes('xác minh') || text.includes('verify')) {
                    try {
                        button.click();
                        return true;
                    } catch (_err) {}
                }
            }

            const submit = root.querySelector('button[type="submit"]');
            if (submit && submit.offsetParent !== null) {
                try {
                    submit.click();
                    return true;
                } catch (_err) {}
            }
            return false;
            """
        )
        if clicked:
            return True
    except Exception:
        pass

    xpaths = [
        '//button[contains(normalize-space(.), "Xác minh")]',
        '//button[contains(normalize-space(.), "Verify")]',
        '//button[@type="submit"]',
    ]
    for xpath in xpaths:
        try:
            nodes = driver.find_elements(By.XPATH, xpath)
        except Exception:
            nodes = []
        for node in nodes:
            try:
                if not node.is_displayed() or not node.is_enabled():
                    continue
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", node)
                try:
                    node.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", node)
                return True
            except Exception:
                continue

    try:
        clicked = driver.execute_script(
            """
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
            for (const button of buttons) {
                if (!button || button.offsetParent === null) continue;
                const text = norm(button.innerText || button.textContent || button.getAttribute('aria-label') || '');
                if (text.includes('xác minh') || text.includes('verify')) {
                    try {
                        button.scrollIntoView({ block: 'center', behavior: 'instant' });
                        button.click();
                        return true;
                    } catch (_err) {}
                }
            }
            const submit = document.querySelector('button[type="submit"]');
            if (submit && submit.offsetParent !== null) {
                try {
                    submit.scrollIntoView({ block: 'center', behavior: 'instant' });
                    submit.click();
                    return true;
                } catch (_err) {}
            }
            return false;
            """
        )
        if clicked:
            return True
    except Exception:
        pass
    return False


def _wait_until(timeout_seconds, predicate, interval=0.2):
    deadline = time.time() + max(0.1, float(timeout_seconds))
    while time.time() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception:
            pass
        time.sleep(interval)
    return None


def _wait_for_2fa_dialog_progress(driver, timeout_seconds=4.0):
    def _probe():
        try:
            body_text = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
        except Exception:
            body_text = ""
        if any(token in body_text for token in ("failed to verify", "thử lại", "mã không hợp lệ", "try again")):
            return "invalid"
        if any(token in body_text for token in ("backup", "mã dự phòng", "i have saved", "đã lưu", "done", "xong")):
            return "verified"
        return None

    return _wait_until(timeout_seconds, _probe, interval=0.2)


def setup_two_factor_auth(driver, password: str, log_func=None):
    """Best-effort bật 2FA cho ChatGPT sau khi checkout mới đã xong."""
    if log_func is None:
        log_func = print

    log_func("   🔐 Bắt đầu setup 2FA...")
    try:
        def is_security_panel_open():
            try:
                current_url = (driver.current_url or "").lower()
            except Exception:
                current_url = ""
            if "#settings/security" in current_url or "/settings/security" in current_url:
                return True

            try:
                body_text = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
            except Exception:
                body_text = ""
            keywords = (
                "security",
                "bảo mật",
                "two-factor authentication",
                "authenticator app",
                "xác thực hai yếu tố",
                "ứng dụng xác thực",
            )
            if any(keyword in body_text for keyword in keywords):
                return True

            try:
                return bool(driver.execute_script(
                    """
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [data-radix-popper-content-wrapper] [role="dialog"]'));
                    const surfaces = dialogs.length ? dialogs : [document.body];
                    const wanted = ['security', 'bảo mật', 'authenticator app', 'ứng dụng xác thực', 'two-factor authentication'];
                    for (const surface of surfaces) {
                        const text = norm(surface.innerText || surface.textContent || '');
                        if (wanted.some((item) => text.includes(item))) return true;
                        const selectedTab = surface.querySelector('[role="tab"][aria-selected="true"], button[data-state="active"], [data-state="active"][role="button"]');
                        const selectedText = norm(selectedTab && (selectedTab.innerText || selectedTab.textContent || selectedTab.getAttribute('aria-label') || ''));
                        if (selectedText.includes('security') || selectedText.includes('bảo mật')) return true;
                    }
                    return false;
                    """
                ))
            except Exception:
                return False

        def open_sidebar_if_needed():
            selectors = [
                (By.XPATH, '//*[self::button or @role="button"][@aria-label="Open sidebar" or @aria-label="Mở sidebar"]'),
                (By.XPATH, '//*[self::button or @role="button"][contains(normalize-space(.), "Open sidebar") or contains(normalize-space(.), "Mở sidebar")]'),
            ]
            for by, selector in selectors:
                try:
                    nodes = driver.find_elements(by, selector)
                except Exception:
                    nodes = []
                for node in nodes:
                    try:
                        if not node.is_displayed() or not node.is_enabled():
                            continue
                        try:
                            node.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", node)
                        time.sleep(1)
                        log_func("   ✅ Đã mở sidebar")
                        return True
                    except Exception:
                        continue
            return False

        def click_settings_entry():
            xpaths = [
                '//*[self::button or self::div or self::a or @role="menuitem" or @role="button"][contains(normalize-space(.), "Settings") or contains(normalize-space(.), "Cài đặt")]',
                '//div[@role="menu"]//*[contains(normalize-space(.), "Settings") or contains(normalize-space(.), "Cài đặt")]',
            ]
            for xpath in xpaths:
                try:
                    nodes = driver.find_elements(By.XPATH, xpath)
                except Exception:
                    nodes = []
                for node in nodes:
                    try:
                        if not node.is_displayed():
                            continue
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", node)
                        try:
                            node.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", node)
                        time.sleep(2)
                        log_func("   ✅ Đã mở Settings / Cài đặt")
                        return True
                    except Exception:
                        continue
            try:
                clicked = driver.execute_script(
                    """
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const nodes = Array.from(document.querySelectorAll('button, [role="button"], [role="menuitem"], a, div'));
                    for (const node of nodes) {
                        if (!node || node.offsetParent === null) continue;
                        const text = norm(node.innerText || node.textContent || node.getAttribute('aria-label') || '');
                        if (!text || (!text.includes('settings') && !text.includes('cài đặt'))) continue;
                        try {
                            node.scrollIntoView({ block: 'center', behavior: 'instant' });
                            node.click();
                            return true;
                        } catch (_err) {}
                    }
                    return false;
                    """
                )
                if clicked:
                    time.sleep(2)
                    log_func("   ✅ Đã mở Settings / Cài đặt bằng JS")
                    return True
            except Exception:
                pass
            return False

        def click_security_entry():
            xpaths = [
                '//*[self::button or self::div or self::a or @role="tab" or @role="button"][contains(normalize-space(.), "Security") or contains(normalize-space(.), "Bảo mật")]',
                '//div[@role="dialog"]//*[self::button or self::div or self::a or @role="tab" or @role="button"][contains(normalize-space(.), "Security") or contains(normalize-space(.), "Bảo mật")]',
            ]
            for xpath in xpaths:
                try:
                    nodes = driver.find_elements(By.XPATH, xpath)
                except Exception:
                    nodes = []
                for node in nodes:
                    try:
                        if not node.is_displayed():
                            continue
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", node)
                        try:
                            node.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", node)
                        time.sleep(2)
                        log_func("   ✅ Đã click tab Security / Bảo mật")
                        return True
                    except Exception:
                        continue
            try:
                clicked = driver.execute_script(
                    """
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const nodes = Array.from(document.querySelectorAll('[role="dialog"] button, [role="dialog"] [role="button"], [role="dialog"] [role="tab"], button, [role="button"], [role="tab"], a, div'));
                    for (const node of nodes) {
                        if (!node || node.offsetParent === null) continue;
                        const text = norm(node.innerText || node.textContent || node.getAttribute('aria-label') || '');
                        if (!text || (!text.includes('security') && !text.includes('bảo mật'))) continue;
                        try {
                            node.scrollIntoView({ block: 'center', behavior: 'instant' });
                            node.click();
                            return true;
                        } catch (_err) {}
                    }
                    return false;
                    """
                )
                if clicked:
                    time.sleep(2)
                    log_func("   ✅ Đã click tab Security / Bảo mật bằng JS")
                    return True
            except Exception:
                pass
            return False

        def click_profile_menu():
            js = """
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const candidates = Array.from(document.querySelectorAll('button, [role="button"], [role="menuitem"], a'));
                for (const el of candidates) {
                    if (!el || el.offsetParent === null) continue;
                    const text = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                    const attrs = norm(
                        (el.getAttribute('data-testid') || '') + ' ' +
                        (el.getAttribute('aria-label') || '') + ' ' +
                        (el.getAttribute('title') || '')
                    );
                    if (
                        attrs.includes('user-menu') ||
                        attrs.includes('account-menu') ||
                        attrs.includes('profile-menu') ||
                        text === '...' ||
                        text.includes('settings') ||
                        text.includes('cài đặt')
                    ) {
                        try { el.click(); return true; } catch (_err) {}
                    }
                }
                return false;
            """
            try:
                if driver.execute_script(js):
                    time.sleep(1.5)
                    log_func("   ✅ Đã thử mở menu tài khoản")
                    return True
            except Exception:
                pass
            return False

        def open_security_settings():
            if is_security_panel_open():
                log_func("   ✅ Popup/tab Security đã mở sẵn, không mở lại")
                return True

            security_url = "https://chatgpt.com/#settings/Security"
            attempts = [
                ("direct_get", lambda: driver.get(security_url)),
                ("assign_hash", lambda: driver.execute_script("window.location.href = arguments[0];", security_url)),
                ("replace_hash", lambda: driver.execute_script("window.location.hash = '#settings/Security'; window.dispatchEvent(new HashChangeEvent('hashchange'));")),
            ]
            for method_name, action in attempts:
                try:
                    action()
                except Exception:
                    continue
                time.sleep(3)
                dismiss_chatgpt_onboarding_if_present(driver, max_rounds=2)
                if is_security_panel_open():
                    log_func(f"   ✅ Đã vào được Security bằng cách {method_name}")
                    return True

            open_sidebar_if_needed()
            click_profile_menu()
            if click_settings_entry():
                dismiss_chatgpt_onboarding_if_present(driver, max_rounds=2)
                click_security_entry()
                if is_security_panel_open():
                    log_func("   ✅ Settings đã mở, chuẩn bị chuyển sang Security")
                    return True

            return False

        def scroll_security_dialog(step=900, rounds=1):
            for _ in range(max(1, int(rounds))):
                try:
                    driver.execute_script(
                        """
                        const step = arguments[0];
                        const targets = [
                          document.querySelector('[role="dialog"] [data-radix-scroll-area-viewport]'),
                          document.querySelector('[role="dialog"] [class*="scroll"]'),
                          document.querySelector('[role="dialog"] [class*="overflow"]'),
                          document.querySelector('[role="dialog"]'),
                          document.querySelector('main'),
                          document.scrollingElement || document.documentElement,
                        ].filter(Boolean);
                        for (const el of targets) {
                          try {
                            el.scrollTop = (el.scrollTop || 0) + step;
                          } catch (_err) {}
                          try {
                            el.scrollBy(0, step);
                          } catch (_err) {}
                        }
                        window.scrollBy(0, step);
                        """,
                        step,
                    )
                except Exception:
                    pass
                time.sleep(0.45)

        def click_authenticator_switch_like_gpt1():
            script = """
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
                let node;
                const lowered = (value) => String(value || '').toLowerCase();
                const isChecked = (el) => {
                    if (!el) return false;
                    const aria = lowered(el.getAttribute && el.getAttribute('aria-checked'));
                    const state = lowered(el.getAttribute && el.getAttribute('data-state'));
                    return aria === 'true' || state === 'checked';
                };
                while ((node = walker.nextNode())) {
                    const text = lowered(node.nodeValue).trim();
                    if (!text.includes('authenticator app') && !text.includes('ứng dụng xác thực')) continue;
                    let parent = node.parentElement;
                    for (let i = 0; i < 6; i++) {
                        if (!parent) break;
                        const toggle = parent.querySelector('button[role="switch"], [role="switch"], input[type="checkbox"]');
                        if (toggle) {
                            try { toggle.scrollIntoView({ block: 'center', behavior: 'instant' }); } catch (_err) {}
                            if (!isChecked(toggle)) {
                                try { toggle.click(); } catch (_err) {
                                    try {
                                        toggle.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                                    } catch (_err2) {}
                                }
                            }
                            return {
                                found: true,
                                clicked: !isChecked(toggle),
                                already_checked: isChecked(toggle),
                                method: 'gpt1-text-node-toggle'
                            };
                        }
                        parent = parent.parentElement;
                    }
                }

                const visibleSwitches = Array.from(document.querySelectorAll('button[role="switch"], [role="switch"], input[type="checkbox"]'))
                    .filter((el) => el && el.offsetParent !== null);
                if (visibleSwitches.length > 0) {
                    const first = visibleSwitches[0];
                    try { first.scrollIntoView({ block: 'center', behavior: 'instant' }); } catch (_err) {}
                    if (!isChecked(first)) {
                        try { first.click(); } catch (_err) {
                            try {
                                first.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                            } catch (_err2) {}
                        }
                    }
                    return {
                        found: true,
                        clicked: !isChecked(first),
                        already_checked: isChecked(first),
                        method: 'gpt1-first-visible-switch'
                    };
                }

                return { found: false, clicked: false, already_checked: false, method: 'none' };
            """
            try:
                result = driver.execute_script(script)
            except Exception:
                return None
            return result if isinstance(result, dict) else None

        def direct_gpt1_open_and_toggle():
            security_url = "https://chatgpt.com/#settings/Security"
            log_func(f"   🌐 Đang mở trực tiếp: {security_url}")
            try:
                driver.get(security_url)
            except Exception:
                return None
            time.sleep(2.2)

            for round_index in range(4):
                if round_index > 0:
                    log_func(f"   🔽 GPT-1 flow cuộn xuống lần {round_index}...")
                scroll_security_dialog(step=900, rounds=1)
                result = click_authenticator_switch_like_gpt1()
                if result and result.get("found"):
                    method = str(result.get("method") or "gpt1-js").strip()
                    if result.get("already_checked"):
                        log_func(f"   ✅ GPT-1 flow thấy switch đã bật ({method})")
                        return result
                    log_func(f"   ✅ GPT-1 flow đã click switch 2FA ({method})")
                    time.sleep(0.8)
                    return result
            return None

        def click_authenticator_switch_via_js():
            script = """
                const lowered = (value) => String(value || "").toLowerCase();
                const isChecked = (el) => {
                    if (!el) return false;
                    const aria = lowered(el.getAttribute && el.getAttribute("aria-checked"));
                    const dataState = lowered(el.getAttribute && el.getAttribute("data-state"));
                    if (aria === "true" || dataState === "checked") return true;
                    return false;
                };
                const clickEl = (el) => {
                    if (!el) return false;
                    try { el.scrollIntoView({ block: "center", behavior: "instant" }); } catch (_err) {}
                    try { el.click(); return true; } catch (_err) {}
                    try {
                        el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
                        return true;
                    } catch (_err) {}
                    return false;
                };
                const findSwitchNear = (root) => {
                    if (!root || !root.querySelectorAll) return null;
                    const candidates = root.querySelectorAll('button[role="switch"], [role="switch"], input[type="checkbox"]');
                    for (const candidate of candidates) {
                        if (candidate && candidate.offsetParent !== null) return candidate;
                    }
                    return null;
                };

                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let node = null;
                while ((node = walker.nextNode())) {
                    const text = lowered(node.nodeValue).trim();
                    if (!text) continue;
                    if (!text.includes("authenticator app") && !text.includes("ứng dụng xác thực")) continue;

                    let parent = node.parentElement;
                    for (let depth = 0; depth < 6 && parent; depth += 1) {
                        const toggle = findSwitchNear(parent);
                        if (toggle) {
                            return {
                                found: true,
                                clicked: isChecked(toggle) ? false : clickEl(toggle),
                                already_checked: isChecked(toggle),
                                method: "text-node-nearby-switch",
                            };
                        }
                        parent = parent.parentElement;
                    }
                }

                const visibleSwitches = Array.from(document.querySelectorAll('button[role="switch"], [role="switch"], input[type="checkbox"]'))
                    .filter((el) => el && el.offsetParent !== null);
                if (visibleSwitches.length > 0) {
                    const first = visibleSwitches[0];
                    return {
                        found: true,
                        clicked: isChecked(first) ? false : clickEl(first),
                        already_checked: isChecked(first),
                        method: "first-visible-switch-fallback",
                    };
                }

                return { found: false, clicked: false, already_checked: false, method: "none" };
            """
            try:
                result = driver.execute_script(script)
            except Exception:
                return None
            return result if isinstance(result, dict) else None

        def find_authenticator_switch():
            xpaths = [
                '//*[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "authenticator app")]',
                '//*[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZÀÁẠẢÃĂẮẰẲẴẶÂẤẦẨẪẬĐÈÉẸẺẼÊẾỀỂỄỆÌÍỊỈĨÒÓỌỎÕÔỐỒỔỖỘƠỚỜỞỠỢÙÚỤỦŨƯỨỪỬỮỰỲÝỴỶỸ", "abcdefghijklmnopqrstuvwxyzàáạảãăắằẳẵặâấầẩẫậđèéẹẻẽêếềểễệìíịỉĩòóọỏõôốồổỗộơớờởỡợùúụủũưứừửữựỳýỵỷỹ"), "ứng dụng xác thực")]',
            ]
            for xpath in xpaths:
                try:
                    labels = driver.find_elements(By.XPATH, xpath)
                except Exception:
                    labels = []
                for label in labels:
                    try:
                        if not label.is_displayed():
                            continue
                        switch = label.find_elements(By.XPATH, './ancestor::*[self::div or self::button][1]//*[@role="switch"]')
                        if switch:
                            return label, switch[0]
                        switch = label.find_elements(By.XPATH, './ancestor::*[self::div or self::button][1]//*[contains(@class, "radix") and (@data-state="checked" or @data-state="unchecked")]')
                        if switch:
                            return label, switch[0]
                    except Exception:
                        continue
            return None, None

        def scroll_security_view(step=700):
            try:
                driver.execute_script(
                    """
                    const step = arguments[0];
                    const targets = [
                      document.querySelector('[role="dialog"] [class*="overflow"]'),
                      document.querySelector('[role="dialog"]'),
                      document.querySelector('main'),
                      document.scrollingElement || document.documentElement,
                    ].filter(Boolean);
                    for (const el of targets) {
                      try {
                        el.scrollTop = (el.scrollTop || 0) + step;
                      } catch (_err) {}
                    }
                    window.scrollBy(0, step);
                    """,
                    step,
                )
            except Exception:
                pass
            time.sleep(0.4)

        direct_toggle_result = direct_gpt1_open_and_toggle()
        if direct_toggle_result and direct_toggle_result.get("found"):
            enable_button = True
        else:
            open_security_settings()
            time.sleep(0.8)
            dismiss_chatgpt_onboarding_if_present(driver, max_rounds=4)
            try:
                log_func(f"   [Debug] URL settings hiện tại: {driver.current_url}")
            except Exception:
                pass

            security_selectors = [
                (By.XPATH, '//*[self::button or self::div or self::a][contains(normalize-space(.), "Security") or contains(normalize-space(.), "Bảo mật")]'),
            ]
            for by, selector in security_selectors:
                try:
                    nodes = driver.find_elements(by, selector)
                except Exception:
                    nodes = []
                for node in nodes:
                    try:
                        if not node.is_displayed():
                            continue
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", node)
                        try:
                            node.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", node)
                        log_func("   ✅ Đã mở tab Security / Bảo mật")
                        time.sleep(0.6)
                        break
                    except Exception:
                        continue

            log_func("   🧭 Cuộn xuống khu vực Security để tìm mục Authenticator app...")
            for warmup_scroll in range(3):
                scroll_security_dialog(step=850, rounds=1)
                scroll_security_view(step=300)
                time.sleep(0.5)

            enable_selectors = [
                (By.XPATH, '//*[self::button or self::a or @role="button" or self::div or self::span][contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "authenticator app")]'),
                (By.XPATH, '//*[self::button or self::a or @role="button" or self::div][contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "enable two-factor authentication")]'),
                (By.XPATH, '//*[self::button or self::a or @role="button" or self::div][contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "set up two-factor authentication")]'),
                (By.XPATH, '//*[self::button or self::a or @role="button" or self::div][contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "two-factor authentication")]'),
                (By.XPATH, '//*[self::button or self::a or @role="button" or self::div][contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZÀÁẠẢÃĂẮẰẲẴẶÂẤẦẨẪẬĐÈÉẸẺẼÊẾỀỂỄỆÌÍỊỈĨÒÓỌỎÕÔỐỒỔỖỘƠỚỜỞỠỢÙÚỤỦŨƯỨỪỬỮỰỲÝỴỶỸ", "abcdefghijklmnopqrstuvwxyzàáạảãăắằẳẵặâấầẩẫậđèéẹẻẽêếềểễệìíịỉĩòóọỏõôốồổỗộơớờởỡợùúụủũưứừửữựỳýỵỷỹ"), "bật xác thực hai yếu tố")]'),
                (By.XPATH, '//*[self::button or self::a or @role="button" or self::div][contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZÀÁẠẢÃĂẮẰẲẴẶÂẤẦẨẪẬĐÈÉẸẺẼÊẾỀỂỄỆÌÍỊỈĨÒÓỌỎÕÔỐỒỔỖỘƠỚỜỞỠỢÙÚỤỦŨƯỨỪỬỮỰỲÝỴỶỸ", "abcdefghijklmnopqrstuvwxyzàáạảãăắằẳẵặâấầẩẫậđèéẹẻẽêếềểễệìíịỉĩòóọỏõôốồổỗộơớờởỡợùúụủũưứừửữựỳýỵỷỹ"), "xác thực hai yếu tố")]'),
            ]
            enable_button = None
            for scroll_round in range(5):
                log_func(f"   🔎 Quét mục 2FA ở vị trí cuộn lần {scroll_round + 1}...")

                gpt1_toggle_result = click_authenticator_switch_like_gpt1()
                if gpt1_toggle_result and gpt1_toggle_result.get("found"):
                    method = str(gpt1_toggle_result.get("method") or "gpt1-js").strip()
                    if gpt1_toggle_result.get("already_checked"):
                        log_func(f"   ✅ JS kiểu GPT-1 xác nhận switch đã bật ({method})")
                        enable_button = True
                        break
                    if gpt1_toggle_result.get("clicked"):
                        log_func(f"   ✅ JS kiểu GPT-1 đã click switch Authenticator app ({method})")
                        enable_button = True
                        break

                js_toggle_result = click_authenticator_switch_via_js()
                if js_toggle_result and js_toggle_result.get("found"):
                    method = str(js_toggle_result.get("method") or "js").strip()
                    if js_toggle_result.get("already_checked"):
                        log_func(f"   ✅ JS đã xác nhận Authenticator app đang bật ({method})")
                        enable_button = True
                        break
                    if js_toggle_result.get("clicked"):
                        log_func(f"   ✅ JS đã click được switch Authenticator app ({method})")
                        enable_button = True
                        break

                auth_label, auth_switch = find_authenticator_switch()
                if auth_switch:
                    try:
                        state = (
                            auth_switch.get_attribute("aria-checked")
                            or auth_switch.get_attribute("data-state")
                            or ""
                        ).strip().lower()
                    except Exception:
                        state = ""
                    if auth_label:
                        try:
                            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", auth_label)
                            time.sleep(0.3)
                        except Exception:
                            pass
                    if state in {"true", "checked"}:
                        log_func("   ✅ Authenticator app đã ở trạng thái bật")
                        enable_button = auth_switch
                        break
                    log_func("   ✅ Tìm thấy dòng Authenticator app, sẽ thử click bật switch")
                    enable_button = auth_switch
                    break

                for by, selector in enable_selectors:
                    try:
                        buttons = driver.find_elements(by, selector)
                    except Exception:
                        buttons = []
                    for button in buttons:
                        try:
                            if button.is_displayed() and button.is_enabled():
                                enable_button = button
                                break
                        except Exception:
                            continue
                    if enable_button:
                        break
                if enable_button:
                    break
                log_func(f"   🔽 Chưa thấy mục bật 2FA, cuộn xuống thêm...")
                scroll_security_dialog(step=950, rounds=2)
                scroll_security_view(step=850)

        if 'enable_button' not in locals():
            enable_button = None
        if not enable_button:
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text or ""
                snippet = "\n".join(line for line in body_text.splitlines() if line.strip())[:1200]
                log_func(f"   [Debug] Nội dung settings/security hiện tại:\n{snippet}")
            except Exception:
                pass
            log_func("   ⚠️ Không tìm thấy nút bật 2FA, có thể đã bật sẵn hoặc UI thay đổi")
            return {"success": False, "reason": "Không tìm thấy nút bật 2FA"}

        if enable_button is not True:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", enable_button)
                time.sleep(0.1)
                enable_button.click()
            except Exception:
                driver.execute_script("arguments[0].click();", enable_button)
            time.sleep(0.8)

            try:
                switch_state = (
                    enable_button.get_attribute("aria-checked")
                    or enable_button.get_attribute("data-state")
                    or ""
                ).strip().lower()
            except Exception:
                switch_state = ""
            if switch_state in {"true", "checked"}:
                log_func("   ✅ Switch Authenticator app đã bật")
        else:
            time.sleep(3)

        password_inputs = _visible_elements(driver, 'input[name="password"], input[type="password"], input[autocomplete="current-password"]')
        if password_inputs:
            log_func("   🔑 2FA yêu cầu xác nhận mật khẩu, đang nhập...")
            robust_fill_input(driver, password_inputs[0], password, label="mật khẩu 2FA")
            click_button_with_retry(driver, 'button[type="submit"]', max_retries=2)
            time.sleep(0.8)

        if _click_manual_2fa_secret_link(driver):
            log_func("   ✅ Đã mở màn secret 2FA dạng chữ")
        else:
            log_func("   ℹ️ Không thấy link manual secret, thử đọc secret trực tiếp từ popup hiện tại")

        secret = _extract_2fa_secret_from_page(driver)
        if not secret:
            log_func("   ⚠️ Không lấy được secret key 2FA từ trang")
            return {"success": False, "reason": "Không lấy được secret key"}

        log_func(f"   ✅ Đã lấy secret 2FA: {secret[:8]}...")
        code_input = find_code_input_fast(driver, timeout=3)
        if not code_input:
            code_candidates = _visible_elements(driver, 'input[inputmode="numeric"], input[autocomplete="one-time-code"], input[type="text"]')
            code_input = code_candidates[0] if code_candidates else None
        if not code_input:
            return {"success": False, "reason": "Không tìm thấy ô nhập mã 2FA"}

        current_ts = int(time.time())
        attempt_schedule = [
            ("current", current_ts),
            ("previous-window", current_ts - 30),
            ("next-window", current_ts + 30),
        ]
        verified = False
        last_code = ""
        for label, timestamp in attempt_schedule:
            totp_code = _generate_totp_code_at(secret, timestamp, interval=30)
            last_code = totp_code
            log_func(f"   🔢 Đang thử mã TOTP ({label}): {totp_code}")
            robust_fill_input(driver, code_input, totp_code, label="mã 2FA")
            if _click_2fa_verify_button(driver):
                log_func("   ✅ Đã bấm nút Xác minh")
            else:
                try:
                    code_input.send_keys(Keys.ENTER)
                except Exception:
                    pass
            verify_state = _wait_for_2fa_dialog_progress(driver, timeout_seconds=3.5)
            if verify_state == "invalid":
                continue
            if verify_state == "verified":
                verified = True
                break
            verified = True
            break

        if not verified:
            return {"success": False, "reason": f"Không verify được mã 2FA local: {last_code}"}

        backup_codes = _collect_2fa_backup_codes(driver)
        if backup_codes:
            log_func(f"   ✅ Đã lấy {len(backup_codes)} backup codes 2FA")
        else:
            log_func("   ℹ️ Chưa đọc được backup codes, có thể UI không hiển thị ngay")

        return {
            "success": True,
            "secret": secret,
            "backup_codes": backup_codes,
            "verified": True,
            "stage": "verified",
        }
    except Exception as e:
        log_func(f"   ❌ Lỗi setup 2FA: {e}")
        return {"success": False, "reason": str(e)}


def classify_after_password_submit(driver):
    try:
        current_url = driver.current_url
    except Exception:
        current_url = ""

    if is_chatgpt_home_ready(driver):
        return "home", current_url

    if "/about-you" in current_url:
        return "about_you", current_url

    if "email-verification/register" in current_url:
        code_input = find_code_input_fast(driver, timeout=0.2)
        if code_input:
            return "otp", current_url
        if find_profile_name_input_fast(driver, timeout=0.2) or find_birthdate_input(driver) or find_age_input(driver):
            return "profile_form", current_url

    if _visible_elements(driver, CODE_INPUT_SELECTOR):
        return "otp", current_url

    if _visible_elements(driver, PROFILE_INPUT_SELECTOR) or find_birthdate_input(driver) or find_age_input(driver):
        return "profile_form", current_url

    alerts = _visible_alert_texts(driver)
    if alerts:
        return "page_error", alerts[0]

    ready_state = _document_ready_state(driver)
    if ready_state != "complete":
        return "loading", ready_state

    return "transitioning", current_url


def fill_signup_form(driver, email: str, password: str):
    """
    Điền form đăng ký
    Tương thích trang đăng nhập/đăng ký thống nhất mới của ChatGPT
    
    Tham số:
        driver: driver trình duyệt
        email: địa chỉ email
        password: mật khẩu
    
    Trả về:
        bool: có điền thành công hay không
    """
    wait = WebDriverWait(driver, MAX_WAIT_TIME)
    
    try:
        setattr(driver, "signup_post_password_state", "")
        print(f"DEBUG: Tiêu đề trang hiện tại: {driver.title}")
        print(f"DEBUG: URL trang hiện tại: {driver.current_url}")
        
        # Kiểm tra có phải trang xác minh Cloudflare không
        if "Just a moment" in driver.title or "Ray ID" in driver.page_source or "Vui lòng chờ" in driver.title:
             print("⚠️ Phát hiện trang xác minh Cloudflare...")
             # Thử chờ
             time.sleep(10)
             if "Just a moment" in driver.title or "Vui lòng chờ" in driver.title:
                 print("  🔄 Thử làm mới trang để vượt xác minh...")
                 driver.refresh()
                 time.sleep(10)
                 
             # Kiểm tra lại và thử click ô xác minh
             try:
                 # Tìm iframe xác minh CF
                 frames = driver.find_elements(By.TAG_NAME, "iframe")
                 for frame in frames:
                     try:
                         driver.switch_to.frame(frame)
                         # ID hoặc class ô xác minh phổ biến
                         checkbox = driver.find_elements(By.CSS_SELECTOR, "#checkbox, .checkbox, input[type='checkbox'], #challenge-stage")
                         if checkbox:
                             print("  🖱️ Thử click ô xác minh...")
                             driver.execute_script("arguments[0].click();", checkbox[0])
                             time.sleep(5)
                         driver.switch_to.default_content()
                     except:
                         driver.switch_to.default_content()
             except: pass

        def has_email_input():
            return bool(driver.find_elements(
                By.CSS_SELECTOR,
                'input[type="email"], input[name="email"], input[autocomplete="email"]'
            ))

        def click_auth_entry_button():
            if has_email_input():
                return True

            dismiss_cookie_banner(driver, timeout=1.5)

            signup_xpaths = [
                '//*[@data-testid="signup-button"]',
                '//a[contains(., "Sign up") or contains(., "Đăng ký")]',
                '//button[contains(., "Sign up") or contains(., "Đăng ký")]',
                '//*[@role="button" and (contains(., "Sign up") or contains(., "Đăng ký"))]',
                '//div[contains(text(), "Sign up") or contains(text(), "Đăng ký")]',
            ]
            login_xpaths = [
                '//*[@data-testid="login-button"]',
                '//a[contains(., "Log in") or contains(., "Login") or contains(., "Đăng nhập")]',
                '//button[contains(., "Log in") or contains(., "Login") or contains(., "Đăng nhập")]',
                '//*[@role="button" and (contains(., "Log in") or contains(., "Login") or contains(., "Đăng nhập"))]',
                '//div[contains(text(), "Log in") or contains(text(), "Login") or contains(text(), "Đăng nhập")]',
            ]

            def try_auth_buttons(label, xpaths, max_clicks=None):
                for xpath in xpaths:
                    for btn in driver.find_elements(By.XPATH, xpath):
                        try:
                            if not btn.is_displayed():
                                continue
                            print(f"  -> Tìm thấy nút {label}: {btn.text or xpath}")

                            def find_current_button():
                                for current_btn in driver.find_elements(By.XPATH, xpath):
                                    if current_btn.is_displayed():
                                        return current_btn
                                return None

                            if double_click_until_auth_page_changes(
                                driver,
                                find_current_button,
                                has_email_input,
                                label=label,
                                max_clicks=max_clicks,
                            ):
                                return True
                        except Exception as e:
                            print(f"  ⚠️ Không click được nút {label}: {e}")
                            continue
                return False

            # Ưu tiên đăng nhập trước. Nếu double-click 3 lần vẫn không chuyển trang thì mới fallback sang đăng ký.
            if try_auth_buttons("đăng nhập", login_xpaths, max_clicks=3):
                return True

            print("  ⚠️ Nút đăng nhập chưa làm trang chuyển sau 3 lần, chuyển sang thử nút đăng ký...")
            if try_auth_buttons("đăng ký", signup_xpaths, max_clicks=3):
                return True

            return False

        # Trang landing cần bấm Đăng ký/Đăng nhập trước rồi mới có ô email.
        print("🔍 Kiểm tra có cần click nút đăng ký/đăng nhập không...")
        if not has_email_input():
            _bring_browser_to_front_for_auth(driver)
            dismiss_cookie_banner(driver, timeout=3)
        entry_clicked_or_ready = False
        for attempt in range(3):
            dismiss_cookie_banner(driver, timeout=0.8)
            if click_auth_entry_button():
                entry_clicked_or_ready = True
                break
            if attempt == 1:
                print("  ⚠️ Ô email chưa hiện, mở trực tiếp trang auth login...")
                driver.get("https://chatgpt.com/auth/login")
                _wait_for_url_or_dom_settle(driver, timeout=4, stable_for=0.6)
            time.sleep(0.4)

        if not entry_clicked_or_ready:
            print("⚠️ Chưa tìm thấy nút đăng ký/đăng nhập, thử tiếp tục chờ ô email...")

        password_input = None
        max_email_attempts = 3
        for email_attempt in range(max_email_attempts):
            if email_attempt > 0:
                print(f"🔁 Không sang bước OTP/mật khẩu, thử lại bước email ({email_attempt + 1}/{max_email_attempts})...")
                try:
                    driver.get("https://chatgpt.com/auth/login")
                except Exception:
                    try:
                        driver.back()
                    except Exception:
                        pass
                time.sleep(0.5)

            # Sau reload/back có thể bị đá về landing. Khi đó phải bấm Đăng ký/Đăng nhập lại
            # trước khi chờ ô email, nếu không sẽ đứng sai trạng thái đến timeout.
            if not has_email_input():
                print("🔎 Chưa ở form email, kiểm tra lại nút Đăng ký/Đăng nhập...")
                _bring_browser_to_front_for_auth(driver)
                dismiss_cookie_banner(driver, timeout=1.2)
                if click_auth_entry_button():
                    _wait_for_url_or_dom_settle(driver, timeout=6, stable_for=0.6)

            print("📧 Đang chờ ô nhập email...")
            email_input = find_visible_element(
                driver,
                'input[type="email"], input[name="email"], input[autocomplete="email"]',
                timeout=10,
            )
            if not email_input:
                print("⚠️ Không thấy ô nhập email, reload auth rồi bấm Đăng nhập lại nếu cần")
                if email_attempt + 1 >= max_email_attempts:
                    break
                driver.get("https://chatgpt.com/auth/login")
                _wait_for_url_or_dom_settle(driver, timeout=6, stable_for=0.6)
                continue

            if BACKGROUND_MODE:
                _move_browser_offscreen(driver)

            print("📝 Đang nhập email...")
            robust_fill_input(driver, email_input, email, label="email")

            actual_value = email_input.get_attribute('value')
            if actual_value == email:
                print(f"✅ Đã nhập email: {email}")
            else:
                print(f"⚠️ Nhập có thể chưa đầy đủ, giá trị thực tế: {actual_value}")

            print("🔘 Click nút tiếp tục...")
            before_continue_url = driver.current_url
            continue_btn = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[type="submit"]'))
            )
            try:
                ActionChains(driver).move_to_element(continue_btn).click().perform()
            except Exception:
                driver.execute_script("arguments[0].click();", continue_btn)
            print("✅ Đã click tiếp tục")
            _wait_for_url_or_dom_settle(driver, previous_url=before_continue_url, timeout=8, stable_for=0.8)

            print("🔍 Kiểm tra bước tiếp theo là OTP hay mật khẩu...")
            code_input = None
            password_input = None
            inline_form_detected = False
            saw_error_page = False
            last_state = ""
            same_state_since = time.time()
            decision_deadline = time.time() + 30
            while time.time() < decision_deadline:
                if check_and_handle_error(driver):
                    saw_error_page = True
                    break

                password_switch_candidates = _find_continue_with_password_candidates(driver)
                if password_switch_candidates:
                    print("🔒 Đang thấy nút 'Tiếp tục với mật khẩu', bắt buộc chuyển sang flow mật khẩu trước OTP...")
                    if click_continue_with_password(driver, timeout=4):
                        _wait_for_url_or_dom_settle(driver, timeout=6, stable_for=0.6)
                        last_state = ""
                        same_state_since = time.time()
                        continue
                    print("⚠️ Nút 'Tiếp tục với mật khẩu' đang hiện nhưng chưa click được, sẽ tiếp tục ưu tiên thử lại")
                    time.sleep(0.25)
                    continue

                state, detail = classify_after_email_continue(driver)
                if state != last_state:
                    if detail:
                        print(f"  Trạng thái sau Continue: {state} | {detail}")
                    else:
                        print(f"  Trạng thái sau Continue: {state}")
                    last_state = state
                    same_state_since = time.time()

                if state == "inline_otp":
                    code_input = find_code_input_fast(driver, timeout=0.5)
                    inline_form_detected = True
                    print("✅ Phát hiện form đăng ký inline email/OTP/hồ sơ")
                    break

                if state == "password_switch":
                    print("🔀 Phát hiện nút 'Tiếp tục với mật khẩu', ưu tiên điền mật khẩu trước OTP...")
                    if click_continue_with_password(driver, timeout=3):
                        _wait_for_url_or_dom_settle(driver, timeout=6, stable_for=0.6)
                        last_state = ""
                        same_state_since = time.time()
                        continue
                    break

                if state == "otp":
                    if _find_continue_with_password_candidates(driver):
                        print("🔒 Dù đã thấy ô OTP nhưng nút 'Tiếp tục với mật khẩu' vẫn còn, tiếp tục ép sang flow mật khẩu")
                        time.sleep(0.25)
                        continue
                    visible_code_inputs = _visible_elements(driver, CODE_INPUT_SELECTOR)
                    code_input = visible_code_inputs[0] if visible_code_inputs else find_code_input_fast(driver, timeout=0.5)
                    break

                if state == "password":
                    visible_password_inputs = _visible_elements(
                        driver,
                        'input[autocomplete="new-password"], input[name="password"], input[type="password"]'
                    )
                    password_input = visible_password_inputs[0]
                    break

                if state == "page_error":
                    saw_error_page = True
                    break

                if state == "auth_oauth_error":
                    print("⚠️ Rơi vào auth/error?error=OAuthCallback, quay lại trang login ban đầu ngay")
                    break

                if state == "auth_entry":
                    print("⚠️ Sau Continue bị trả về trang có nút Đăng ký/Đăng nhập, sẽ bấm lại và nhập mail lại")
                    break

                if state == "email_still_visible" and detail and time.time() - same_state_since >= 4:
                    break

                if state == "email_still_visible" and not detail and time.time() - same_state_since >= 12:
                    break

                if state == "home" and time.time() - same_state_since >= 8:
                    break

                time.sleep(0.25)

            if code_input or inline_form_detected:
                print("✅ Trang đã chuyển sang bước OTP, bỏ qua bước mật khẩu")
                return True

            if password_input:
                break

            if email_attempt + 1 < max_email_attempts:
                if saw_error_page:
                    print("⚠️ Gặp trang lỗi sau Continue, reload auth rồi nhập lại email")
                elif last_state == "auth_oauth_error":
                    print("⚠️ Gặp OAuthCallback error, quay lại auth/login rồi nhập lại email")
                elif last_state == "auth_entry":
                    print("⚠️ Trang quay lại landing sau Continue, nhập lại từ bước Đăng nhập")
                else:
                    print("⚠️ Chưa thấy OTP/mật khẩu sau 30 giây, reload auth rồi thực hiện lại bước email")
                try:
                    if last_state == "auth_entry":
                        # Giữ nguyên trang landing hiện tại để vòng sau bấm lại Đăng nhập.
                        _wait_for_url_or_dom_settle(driver, timeout=4, stable_for=0.5)
                    elif last_state == "auth_oauth_error":
                        driver.get("https://chatgpt.com/auth/login")
                        _wait_for_url_or_dom_settle(driver, timeout=8, stable_for=0.8)
                    else:
                        driver.get("https://chatgpt.com/auth/login")
                        _wait_for_url_or_dom_settle(driver, timeout=8, stable_for=0.8)
                except Exception:
                    pass
                continue

            if saw_error_page:
                print("⚠️ Gặp trang lỗi sau Continue sau nhiều lần retry")
            else:
                print("⚠️ Không thấy OTP/mật khẩu sau Continue sau nhiều lần retry")

        if not password_input:
            print("❌ Không thấy ô OTP hoặc ô mật khẩu sau khi retry bước email")
            return False

        # 4. Nhập mật khẩu nếu trang vẫn dùng flow cũ
        print("🔑 Trang vẫn yêu cầu mật khẩu, đang nhập mật khẩu...")
        password_input.clear()
        robust_fill_input(driver, password_input, password, label="mật khẩu")
        print("✅ Đã nhập mật khẩu")
        
        # 5. Click tiếp tục
        print("🔘 Click nút tiếp tục...")
        if not click_button_with_retry(driver, 'button[type="submit"]'):
            print("❌ Click nút tiếp tục thất bại")
            return False
        print("✅ Đã click tiếp tục")

        check_and_handle_error(driver)
        post_password_deadline = time.time() + 15
        last_state = ""
        while time.time() < post_password_deadline:
            state, detail = classify_after_password_submit(driver)
            if state != last_state:
                print(f"  Trạng thái sau mật khẩu: {state}" + (f" | {detail}" if detail else ""))
                last_state = state

            if state in ("home", "about_you", "profile_form"):
                setattr(driver, "signup_post_password_state", "logged_in_no_otp")
                print("✅ Account đã vào đúng giao diện sau bước mật khẩu, không cần OTP")
                return True

            if state == "otp":
                setattr(driver, "signup_post_password_state", "otp_required")
                print("✅ Sau bước mật khẩu vẫn yêu cầu OTP email")
                return True

            if state == "page_error":
                break

            time.sleep(0.35)
        
        setattr(driver, "signup_post_password_state", "otp_required")
        return True
        
    except Exception as e:
        print(f"❌ Điền form thất bại: {e}")
        return False



def login(driver, email, password):
    """
    Đăng nhập ChatGPT
    """
    print(f"🔐 Đang đăng nhập {email}...")
    wait = WebDriverWait(driver, 30)
    
    try:
        driver.get("https://chat.openai.com/auth/login")
        time.sleep(5)
        
        # 0. Double-click nút Log in / đăng nhập ở trang ban đầu
        print("🔘 Tìm nút Log in / đăng nhập...")
        try:
            # Thử nhiều selector, hỗ trợ giao diện tiếng Trung
            xpaths = [
                '//button[@data-testid="login-button"]',
                '//button[contains(., "Log in")]',
                '//div[contains(text(), "Log in")]'
            ]
            
            def find_login_btn():
                for xpath in xpaths:
                    try:
                        btns = driver.find_elements(By.XPATH, xpath)
                        for btn in btns:
                            if btn.is_displayed():
                                return btn
                    except:
                        continue
                return None
            
            login_btn = find_login_btn()
            if login_btn:
                double_click_until_auth_page_changes(
                    driver,
                    find_login_btn,
                    lambda: bool(driver.find_elements(By.CSS_SELECTOR, 'input[name="username"], input[name="email"], input[id="email-input"], input[type="email"]')),
                    label="đăng nhập",
                )
            else:
                print("⚠️ Không tìm thấy nút đăng nhập rõ ràng, thử tìm thẳng ô nhập")
        except Exception as e:
            print(f"⚠️ Lỗi khi click nút đăng nhập: {e}")
            
        time.sleep(3)
        
        # 1. Nhập email
        print("📧 Nhập email...")
        # Tăng thời gian chờ
        email_input = wait.until(EC.visibility_of_element_located((
            By.CSS_SELECTOR, 
            'input[name="username"], input[name="email"], input[id="email-input"]'
        )))
        email_input.clear()
        type_slowly(email_input, email)
        
        # Click tiếp tục
        print("🔘 Click tiếp tục...")
        continue_btn = driver.find_element(By.CSS_SELECTOR, 'button[type="submit"], button[class*="continue-btn"]')
        continue_btn.click()
        time.sleep(3)
        
        # Sửa quan trọng: kiểm tra có vào chế độ mã xác minh không, nếu có thì chuyển lại chế độ mật khẩu
        print("🔍 Kiểm tra cách đăng nhập...")
        try:
            # Tìm mọi phần tử văn bản chứa mật khẩu hoặc Password nếu trông giống link/nút
            # Loại trừ label của chính ô nhập mật khẩu
            switch_candidates = driver.find_elements(By.XPATH, 
                '//*[contains(text(), "mật khẩu") or contains(text(), "Password")]'
            )
            
            clicked_switch = False
            for el in switch_candidates:
                if not el.is_displayed():
                    continue
                    
                tag_name = el.tag_name.lower()
                text = el.text
                
                # Loại trừ label và title
                if tag_name in ['h1', 'h2', 'label', 'span'] and 'Enter' not in text:
                    continue
                    
                # Thử click phần tử trông giống link chuyển chế độ
                if 'Enter password' in text or 'password instead' in text:
                    print(f"⚠️ Thử click link chuyển chế độ: '{text}' ({tag_name})...")
                    try:
                        el.click()
                        clicked_switch = True
                        time.sleep(2)
                        break
                    except:
                        # Có thể bị che, thử click bằng JS
                        driver.execute_script("arguments[0].click();", el)
                        clicked_switch = True
                        time.sleep(2)
                        break
            
            if not clicked_switch:
                print("  ℹ️ Không tìm thấy link chuyển sang mật khẩu rõ ràng, giả định đang ở trang nhập mật khẩu hoặc trang bắt buộc mã xác minh")
                
        except Exception as e:
            print(f"  Lỗi khi kiểm tra cách đăng nhập: {e}")
        
        # 2. Nhập mật khẩu
        print("🔑 Đang chờ ô nhập mật khẩu...")
        try:
            password_input = wait.until(EC.visibility_of_element_located((
                By.CSS_SELECTOR, 
                'input[name="password"], input[type="password"]'
            )))
            password_input.clear()
            type_slowly(password_input, password)
            
            # Click tiếp tục/đăng nhập
            print("🔘 Click đăng nhập...")
            continue_btn = driver.find_element(By.CSS_SELECTOR, 'button[type="submit"], button[name="action"]')
            continue_btn.click()
            
            print("⏳ Chờ đăng nhập hoàn tất...")
            time.sleep(10)
        
        except Exception as e:
            print("❌ Không tìm thấy ô nhập mật khẩu.")
            print("  Nguyên nhân có thể: 1. Bắt buộc đăng nhập bằng mã xác minh; 2. Trang tải chậm; 3. Selector không còn đúng")
            print("  Hãy thử can thiệp thủ công hoặc kiểm tra trang...")
            raise e # Ném exception để dừng kiểm thử
        
        # Kiểm tra đăng nhập có thành công không
        if "auth" not in driver.current_url:
            print("✅ Đăng nhập thành công")
            return True
        else:
            print("⚠️ Có thể vẫn ở trang đăng nhập, URL chứa auth")
            # Kiểm tra lại xem có thông báo lỗi không
            try:
                err = driver.find_element(By.CSS_SELECTOR, '.error-message, [role="alert"]')
                print(f"❌Thông báo lỗi đăng nhập: {err.text}")
            except:
                pass
            return True
            
    except Exception as e:
        print(f"❌ Đăng nhập thất bại: {e}")
        return False


def enter_verification_code(driver, code: str):
    """
    Nhập mã xác minh
    
    Tham số:
        driver: driver trình duyệt
        code: mã xác minh
    
    Trả về:
        "accepted" nếu đã sang bước hồ sơ, "retry" nếu vẫn ở OTP,
        "inline_retry" nếu lỗi ở form gộp OTP/hồ sơ, "profile_error" nếu trang lỗi sau OTP,
        "failed" nếu lỗi rõ ràng.
    """
    try:
        print("🔢 Đang nhập mã xác minh...")
        
        # Kiểm tra lỗi rõ ràng trước, không quét page source rộng để tránh false-positive.
        check_and_handle_error(driver)
        
        code_input = find_code_input_fast(driver, timeout=4)
        if not code_input:
            print("❌ Không thấy ô nhập OTP dù API đã có code")
            return "failed"

        if not robust_fill_input(driver, code_input, code, label="OTP"):
            print("⚠️ OTP có thể chưa được nhập đủ, thử tiếp tục kiểm tra trang")
        print(f"✅ Đã nhập mã xác minh: {code}")

        was_inline_registration = fill_inline_registration_profile_if_present(driver)
        
        # Click tiếp tục
        print("🔘 Click nút tiếp tục...")
        if not click_button_with_retry(driver, 'button[type="submit"]'):
            print("❌ Click nút tiếp tục thất bại")
            if was_inline_registration or is_inline_registration_form(driver):
                return "inline_retry"
            return "failed"
        print("✅ Đã click tiếp tục")

        try:
            post_submit_url = driver.current_url
        except Exception:
            post_submit_url = ""
        _wait_for_url_or_dom_settle(driver, previous_url=post_submit_url, timeout=10, stable_for=1.2)

        deadline = time.time() + OTP_POST_SUBMIT_TRANSITION_TIMEOUT
        last_state = ""
        same_state_since = time.time()
        while time.time() < deadline:
            check_and_handle_error(driver)

            state, detail = classify_after_otp_submit(driver)
            if state != last_state:
                if detail:
                    print(f"  Trạng thái sau OTP: {state} | {detail}")
                else:
                    print(f"  Trạng thái sau OTP: {state}")
                last_state = state
                same_state_since = time.time()

            if state == "home":
                print("✅ OTP/form inline được chấp nhận, đã vào trang chủ ChatGPT")
                return "accepted"

            if state == "about_you":
                print("✅ OTP được chấp nhận, URL đã sang about-you")
                return "accepted"

            if state in ("profile_form", "inline_profile"):
                print("✅ OTP được chấp nhận, đã sang bước điền họ tên")
                return "accepted"

            if state == "otp_invalid":
                print(f"⚠️ Trang báo lỗi OTP: {detail}")
                if was_inline_registration or is_inline_registration_form(driver):
                    return "inline_retry"
                return "retry"

            if state == "page_error":
                print(f"⚠️ Trang báo lỗi sau OTP: {detail}")
                if was_inline_registration or is_inline_registration_form(driver):
                    return "inline_retry"
                return "profile_error"

            if state == "otp_visible":
                time.sleep(0.35)
                continue

            if state == "transitioning":
                # Sau submit OTP có thể đứng vài giây ở giữa luồng; chưa nên kết luận fail/retry.
                time.sleep(0.5)
                continue

            if state == "loading":
                time.sleep(0.5)
                continue

            # Nếu ô OTP đã biến mất và không có lỗi, cho trang thêm thời gian render.
            if time.time() - same_state_since < 3:
                time.sleep(0.35)
                continue

            time.sleep(0.35)
        
        state, detail = classify_after_otp_submit(driver)
        if state in ("home", "about_you", "profile_form", "inline_profile", "transitioning"):
            print("✅ OTP form không còn hiển thị sau khi chờ, chuyển sang bước hồ sơ")
            return "accepted"

        print(f"⚠️ Sau khi nhập OTP {OTP_POST_SUBMIT_TRANSITION_TIMEOUT}s vẫn chưa sang bước họ tên, cần lấy lại OTP")
        if was_inline_registration or is_inline_registration_form(driver):
            return "inline_retry"
        return "retry"
        
    except Exception as e:
        print(f"❌ Nhập mã xác minh thất bại: {e}")
        return "failed"


def find_age_input(driver):
    """Tìm ô nhập tuổi ở form hồ sơ mới của ChatGPT."""
    candidates = []
    try:
        candidates.extend(driver.find_elements(By.CSS_SELECTOR, AGE_INPUT_SELECTOR))
    except Exception:
        pass

    xpath = (
        '//label[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "age") '
        'or contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "tuổi")]'
        '/following::input[1]'
    )
    try:
        candidates.extend(driver.find_elements(By.XPATH, xpath))
    except Exception:
        pass

    seen = set()
    for el in candidates:
        try:
            if el.id in seen or not el.is_displayed() or not el.is_enabled():
                continue
            seen.add(el.id)

            attrs = " ".join(
                str(el.get_attribute(attr) or "")
                for attr in ("name", "id", "placeholder", "aria-label", "autocomplete", "data-type")
            ).lower()
            if any(skip in attrs for skip in ("email", "password", "code", "token", "search")):
                continue
            data_type = str(el.get_attribute("data-type") or "").lower()
            if data_type in ("year", "month", "day") or any(date_kw in attrs for date_kw in ("bday", "birth", "birthday", "date of birth", "ngày sinh", "dd/mm")):
                continue
            if "age" in attrs or "tuổi" in attrs:
                return el
        except Exception:
            continue

    return None


def find_birthdate_input(driver):
    """Tìm ô ngày sinh dạng một input cần nhập dd/mm/yyyy."""
    candidates = []
    try:
        candidates.extend(driver.find_elements(By.CSS_SELECTOR, BIRTHDATE_INPUT_SELECTOR))
    except Exception:
        pass

    xpath = (
        '//label[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "birth") '
        'or contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "birthday") '
        'or contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "date of birth") '
        'or contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "ngày sinh")]'
        '/following::input[1]'
    )
    try:
        candidates.extend(driver.find_elements(By.XPATH, xpath))
    except Exception:
        pass

    seen = set()
    for el in candidates:
        try:
            if el.id in seen or not el.is_displayed() or not el.is_enabled():
                continue
            seen.add(el.id)
            attrs = " ".join(
                str(el.get_attribute(attr) or "")
                for attr in ("name", "id", "placeholder", "aria-label", "autocomplete", "data-type")
            ).lower()
            if any(skip in attrs for skip in ("email", "password", "code", "token", "search")):
                continue
            if any(date_kw in attrs for date_kw in ("birth", "birthday", "bday", "date of birth", "ngày sinh", "dd/mm")):
                return el
        except Exception:
            continue

    return None


def profile_age_value():
    """Tuổi hồ sơ: 2 chữ số và lớn hơn 30."""
    return str(random.randint(31, 40))


def recent_birth_year_value():
    """Ép năm sinh > 2000 cho các UI hồ sơ mới."""
    return str(random.randint(2001, 2006))


def normalized_profile_birthdate(user_info):
    """Chuẩn hóa ngày sinh để không rơi về năm mặc định hiện tại của UI."""
    return {
        "day": str(user_info["day"]).zfill(2),
        "month": str(user_info["month"]).zfill(2),
        "year": recent_birth_year_value(),
    }


def birthdate_ddmmyyyy(user_info):
    birth = normalized_profile_birthdate(user_info)
    return f"{birth['day']}/{birth['month']}/{birth['year']}"


def accept_profile_agreements_if_present(driver):
    """Tick checkbox đồng ý điều khoản ở form hồ sơ nếu giao diện yêu cầu."""
    clicked_any = False
    selectors = [
        'input[name="allCheckboxes"][type="checkbox"]',
        'input[id$="-allCheckboxes"][type="checkbox"]',
        'label:has(input[type="checkbox"])',
    ]

    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    # Selenium CSS trên Chrome hỗ trợ :has, nhưng vẫn có fallback XPath bên dưới.
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            elements = []
        for el in elements:
            try:
                target = el
                checkbox = el
                if (el.tag_name or "").lower() != "input":
                    label_text = (el.text or "").strip().lower()
                    if not any(keyword in label_text for keyword in ("tôi đồng ý", "i agree", "agree to")):
                        continue
                    checkbox = el.find_element(By.CSS_SELECTOR, 'input[type="checkbox"]')
                if checkbox.is_selected():
                    continue
                scroll_element_and_ancestors_into_view(driver, target)
                try:
                    target.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", checkbox)
                time.sleep(0.2)
                if checkbox.is_selected():
                    print("✅ Đã tick checkbox đồng ý điều khoản")
                    clicked_any = True
            except Exception:
                continue

    xpaths = [
        '//label[.//input[@type="checkbox"] and contains(normalize-space(.), "Tôi đồng ý")]',
        '//label[.//input[@type="checkbox"] and contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "i agree")]',
        '//input[@type="checkbox" and (@name="allCheckboxes" or contains(@id, "allCheckboxes"))]',
    ]
    for xpath in xpaths:
        try:
            elements = driver.find_elements(By.XPATH, xpath)
        except Exception:
            elements = []
        for el in elements:
            try:
                checkbox = el
                if (el.tag_name or "").lower() != "input":
                    checkbox = el.find_element(By.CSS_SELECTOR, 'input[type="checkbox"]')
                if checkbox.is_selected():
                    continue
                scroll_element_and_ancestors_into_view(driver, el)
                try:
                    el.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", checkbox)
                time.sleep(0.2)
                if checkbox.is_selected():
                    print("✅ Đã tick checkbox đồng ý điều khoản")
                    clicked_any = True
            except Exception:
                continue

    return clicked_any


def dismiss_chatgpt_onboarding_if_present(driver, max_rounds=5):
    """Dọn các popup/onboarding sau khi account vừa tạo để có thể mở pricing sạch."""
    clicked_any = False

    def scroll_page_to_bottom():
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.25)
            return True
        except Exception:
            return False

    def scroll_page_to_bottom_max():
        moved = False
        for _ in range(6):
            if scroll_page_to_bottom():
                moved = True
            try:
                ActionChains(driver).scroll_by_amount(0, 700).perform()
                moved = True
            except Exception:
                pass
            time.sleep(0.12)
        return moved

    def get_body_text():
        try:
            return driver.execute_script("return document.body.innerText || ''") or ""
        except Exception:
            return ""

    def has_visible_home_promo():
        try:
            return bool(driver.execute_script(
                """
                const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const nodes = [...document.querySelectorAll('button.button-glimmer-cta,button,[role="button"],a')];
                return nodes.some(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.bottom < 0 || r.top > window.innerHeight) return false;
                    const text = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                    return (
                        (text === 'ưu đãi miễn phí' || text.includes('ưu đãi miễn phí') || text === 'free offer' || text.includes('free offer'))
                        && !text.includes('nhận ưu đãi miễn phí')
                    );
                });
                """
            ))
        except Exception:
            return False

    def click_first_matching(xpaths, need_bottom_scroll=False, log_prefix="  🧹 Đã đóng/onboarding"):
        for xpath in xpaths:
            try:
                candidates = driver.find_elements(By.XPATH, xpath)
            except Exception:
                candidates = []
            for el in candidates:
                try:
                    if not el.is_displayed() or not el.is_enabled():
                        continue
                    text = (el.text or el.get_attribute("aria-label") or "").strip()
                    if need_bottom_scroll:
                        scroll_page_to_bottom()
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
                    try:
                        el.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", el)
                    print(f"{log_prefix}: {text or xpath}")
                    time.sleep(0.6)
                    return True
                except Exception:
                    continue
        return False

    def handle_motivation_screen():
        body_text = get_body_text()
        lowered = body_text.lower()
        if "điều gì thôi thúc bạn sử dụng chatgpt" not in lowered and "what brings you to chatgpt" not in lowered:
            return False

        # Khôi phục flow cũ: ưu tiên Bỏ qua qua XPath, nếu không có thì chọn 1 option rồi bấm Tiếp theo.
        scroll_page_to_bottom_max()
        motivation_xpaths = [
            '//button[contains(normalize-space(.), "Bỏ qua")]',
            '//*[self::button or @role="button"][contains(normalize-space(.), "Bỏ qua")]',
            '//button[contains(normalize-space(.), "Skip")]',
            '//*[self::button or @role="button"][contains(normalize-space(.), "Skip")]',
        ]
        for pass_index in range(3):
            if pass_index >= 1:
                scroll_page_to_bottom_max()
            if click_first_matching(
                motivation_xpaths,
                need_bottom_scroll=True,
                log_prefix="  🧹 Đã bỏ qua màn mục đích sử dụng",
            ):
                return True
        try:
            selected = driver.execute_script(
                """
                const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const blockerWords = ['bỏ qua', 'skip', 'tiếp theo', 'next'];
                const seen = new Set();
                const nodes = ['button', '[role="button"]', '[role="radio"]', '[aria-pressed]', '[data-testid]']
                    .flatMap(sel => [...document.querySelectorAll(sel)])
                    .filter(el => {
                        if (seen.has(el)) return false;
                        seen.add(el);
                        const text = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                        const rect = el.getBoundingClientRect();
                        if (!text || rect.width < 40 || rect.height < 24) return false;
                        if (rect.bottom < 0 || rect.top > window.innerHeight) return false;
                        if (blockerWords.some(word => text.includes(word))) return false;
                        return (
                            text.includes('trường học')
                            || text.includes('school')
                            || text.includes('công việc')
                            || text.includes('work')
                            || text.includes('cá nhân')
                            || text.includes('personal')
                            || text.includes('khác')
                            || text.includes('other')
                        );
                    });
                const target = nodes[0];
                if (!target) return false;
                target.scrollIntoView({block: 'center', inline: 'center'});
                target.click();
                return true;
                """
            )
            if selected:
                time.sleep(0.35)
                if click_first_matching(
                    [
                        '//button[contains(normalize-space(.), "Tiếp theo")]',
                        '//*[self::button or @role="button"][contains(normalize-space(.), "Tiếp theo")]',
                        '//button[contains(normalize-space(.), "Next")]',
                        '//*[self::button or @role="button"][contains(normalize-space(.), "Next")]',
                    ],
                    need_bottom_scroll=True,
                    log_prefix="  🧹 Đã đi qua màn mục đích sử dụng",
                ):
                    return True
        except Exception:
            pass
        return False

    def handle_chat_examples_screen():
        body_text = get_body_text()
        lowered = body_text.lower()
        markers = (
            "đoạn chat ví dụ",
            "hỏi gì cũng được",
            "đặt câu hỏi bất kỳ",
            "đừng chia sẻ thông tin nhạy cảm",
            "kiểm tra thông tin của bạn",
            "bỏ qua tìm hiểu",
            "example chat",
            "ask anything",
            "skip intro",
        )
        if not any(marker in lowered for marker in markers):
            return False
        if click_first_matching(
            [
                '//button[contains(normalize-space(.), "Bỏ qua tìm hiểu")]',
                '//button[contains(normalize-space(.), "Bỏ qua tìm hiểu")]',
                '//button[contains(normalize-space(.), "Bỏ qua")]',
                '//button[contains(normalize-space(.), "Skip intro")]',
                '//button[contains(normalize-space(.), "Skip")]',
            ],
            need_bottom_scroll=True,
            log_prefix="  🧹 Đã bỏ qua màn ví dụ/onboarding",
        ):
            return True
        return click_first_matching(
            [
                '//button[contains(normalize-space(.), "Tiếp theo")]',
                '//button[contains(normalize-space(.), "Next")]',
            ],
            need_bottom_scroll=True,
            log_prefix="  🧹 Đã đi tiếp màn ví dụ/onboarding",
        )

    def handle_all_done_screen():
        body_text = get_body_text()
        lowered = body_text.lower()
        if "bạn đã hoàn tất" not in lowered and "you're all set" not in lowered and "you are all set" not in lowered:
            return False
        return click_first_matching(
            [
                '//button[contains(normalize-space(.), "Tiếp tục")]',
                '//button[contains(normalize-space(.), "Continue")]',
                '//button[contains(normalize-space(.), "Tiếp theo")]',
                '//button[contains(normalize-space(.), "Next")]',
            ],
            need_bottom_scroll=True,
            log_prefix="  🧹 Đã tiếp tục màn hoàn tất",
        )

    def handle_tips_modal_screen():
        body_text = get_body_text()
        lowered = body_text.lower()
        markers = (
            "lời khuyên để bắt đầu",
            "ok, tiến hành thôi",
            "kiểm tra thông tin của bạn",
            "chatgpt có thể trả lời các câu hỏi",
            "tips to get started",
        )
        if not any(marker in lowered for marker in markers):
            return False
        if click_first_matching(
            [
                '//button[contains(normalize-space(.), "Bỏ qua tìm hiểu")]',
                '//button[contains(normalize-space(.), "Bỏ qua")]',
                '//button[contains(normalize-space(.), "Skip intro")]',
                '//button[contains(normalize-space(.), "Skip")]',
            ],
            need_bottom_scroll=False,
            log_prefix="  🧹 Đã bỏ qua màn tips/modal",
        ):
            return True
        return click_first_matching(
            [
                '//button[contains(normalize-space(.), "OK, tiến hành thôi")]',
                '//button[contains(normalize-space(.), "Okay, let\'s go")]',
                '//button[contains(normalize-space(.), "Tiếp tục")]',
                '//button[contains(normalize-space(.), "Continue")]',
            ],
            need_bottom_scroll=False,
            log_prefix="  🧹 Đã tiếp tục màn tips/modal",
        )

    button_xpaths = [
        '//button[contains(normalize-space(.), "Bỏ qua tìm hiểu")]',
        '//button[contains(normalize-space(.), "Bỏ qua")]',
        '//button[contains(normalize-space(.), "Skip")]',
        '//button[contains(normalize-space(.), "Đã hiểu")]',
        '//button[contains(normalize-space(.), "OK, tiến hành thôi")]',
        '//button[contains(normalize-space(.), "Okay")]',
        '//button[contains(normalize-space(.), "Got it")]',
    ]

    for _ in range(max_rounds):
        try:
            driver.switch_to.default_content()
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        except Exception:
            pass
        clicked_this_round = False

        if has_visible_home_promo():
            break

        if handle_motivation_screen():
            clicked_any = True
            clicked_this_round = True
        elif handle_chat_examples_screen():
            clicked_any = True
            clicked_this_round = True
        elif handle_all_done_screen():
            clicked_any = True
            clicked_this_round = True
        elif handle_tips_modal_screen():
            clicked_any = True
            clicked_this_round = True

        for xpath in ([] if clicked_this_round else button_xpaths):
            if has_visible_home_promo():
                break
            try:
                buttons = driver.find_elements(By.XPATH, xpath)
            except Exception:
                buttons = []
            for btn in buttons:
                try:
                    if not btn.is_displayed() or not btn.is_enabled():
                        continue
                    text = (btn.text or btn.get_attribute("aria-label") or "").strip()
                    if text and ("nâng cấp" in text.lower() or "ưu đãi miễn phí" in text.lower()):
                        continue
                    if any(marker in text.lower() for marker in ("tiếp tục", "tiếp theo", "continue", "next", "bỏ qua", "skip")):
                        scroll_page_to_bottom()
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    try:
                        btn.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", btn)
                    print(f"  🧹 Đã đóng/onboarding: {text or xpath}")
                    clicked_any = True
                    clicked_this_round = True
                    time.sleep(0.6)
                    break
                except Exception:
                    continue
            if clicked_this_round:
                break

        if not clicked_this_round:
            try:
                driver.execute_script(
                    """
                    const sel = window.getSelection && window.getSelection();
                    if (sel) sel.removeAllRanges();
                    document.activeElement && document.activeElement.blur && document.activeElement.blur();
                    """
                )
            except Exception:
                pass
            break

    return clicked_any


def is_inline_registration_form(driver):
    """Nhận diện form gộp OTP + hồ sơ trên màn email-verification/register."""
    try:
        current_url = driver.current_url
    except Exception:
        current_url = ""

    if "email-verification/register" in current_url:
        return True

    return bool(find_profile_name_input_fast(driver, timeout=0.3) or find_birthdate_input(driver) or find_age_input(driver))


def fill_inline_registration_profile_if_present(driver):
    """
    Luồng mới của auth.openai.com có thể để OTP, họ tên và tuổi trên cùng một trang.
    Nếu thấy các field hồ sơ tại màn OTP thì điền luôn trước khi submit.
    """
    try:
        current_url = driver.current_url
    except Exception:
        current_url = ""

    has_inline_url = "email-verification/register" in current_url
    name_input = find_profile_name_input_fast(driver, timeout=1.5)
    birthdate_input = find_birthdate_input(driver)
    age_input = find_age_input(driver)

    if not has_inline_url and not name_input and not birthdate_input and not age_input:
        return False

    user_info = generate_user_info()
    normalized_birth = normalized_profile_birthdate(user_info)
    birthdate_text = birthdate_ddmmyyyy(user_info)
    filled_any = False

    if name_input:
        try:
            current_name = (name_input.get_attribute("value") or "").strip()
        except Exception:
            current_name = ""
        if not current_name:
            scroll_element_and_ancestors_into_view(driver, name_input)
            robust_fill_input(driver, name_input, user_info["name"], label="họ tên")
            print(f"✅ Đã nhập họ tên trên form inline: {user_info['name']}")
            filled_any = True
        set_registered_profile_name(driver, user_info["name"])

    if birthdate_input:
        try:
            current_birthdate = (birthdate_input.get_attribute("value") or "").strip()
        except Exception:
            current_birthdate = ""
        if not current_birthdate:
            scroll_element_and_ancestors_into_view(driver, birthdate_input)
            if fill_birthdate_ddmmyyyy_input(driver, birthdate_input, birthdate_text):
                print(f"✅ Đã nhập ngày sinh trên form inline: {birthdate_text}")
                filled_any = True
            else:
                print(f"⚠️ Nhập ngày sinh trên form inline chưa đạt format DD/MM/YYYY: {birthdate_text}")
    elif age_input:
        try:
            current_age = (age_input.get_attribute("value") or "").strip()
        except Exception:
            current_age = ""
        if not current_age:
            profile_age = profile_age_value()
            scroll_element_and_ancestors_into_view(driver, age_input)
            robust_fill_input(driver, age_input, profile_age, label="tuổi")
            print(f"✅ Đã nhập tuổi trên form inline: {profile_age}")
            filled_any = True
    else:
        try:
            year_input = driver.find_element(By.CSS_SELECTOR, '[data-type="year"]')
            month_input = driver.find_element(By.CSS_SELECTOR, '[data-type="month"]')
            day_input = driver.find_element(By.CSS_SELECTOR, '[data-type="day"]')
            scroll_element_and_ancestors_into_view(driver, year_input)
            fill_text_fast(driver, year_input, normalized_birth["year"])
            fill_text_fast(driver, month_input, normalized_birth["month"])
            fill_text_fast(driver, day_input, normalized_birth["day"])
            print(f"✅ Đã nhập ngày sinh trên form inline: {birthdate_text}")
            filled_any = True
        except Exception:
            pass

    if accept_profile_agreements_if_present(driver):
        filled_any = True

    return filled_any


def fill_profile_info(driver):
    """
    Điền hồ sơ người dùng bằng tên và tuổi/ngày sinh ngẫu nhiên
    
    Tham số:
        driver: driver trình duyệt
    
    Trả về:
        bool: có thành công hay không
    """
    wait = WebDriverWait(driver, MAX_WAIT_TIME)
    
    # Tạo thông tin người dùng ngẫu nhiên
    user_info = generate_user_info()
    user_name = user_info['name']
    normalized_birth = normalized_profile_birthdate(user_info)
    birthdate_text = birthdate_ddmmyyyy(user_info)
    birthday_year = normalized_birth['year']
    birthday_month = normalized_birth['month']
    birthday_day = normalized_birth['day']
    
    try:
        def scroll_profile_page_to_bottom_max():
            try:
                for _ in range(6):
                    driver.execute_script(
                        """
                        const targets = [document.scrollingElement || document.documentElement, ...document.querySelectorAll('main, [role="main"], [class*="overflow"], [style*="overflow"]')];
                        for (const el of targets) {
                            if (!el) continue;
                            try { el.scrollTop = el.scrollHeight; } catch (e) {}
                        }
                        window.scrollTo(0, document.body.scrollHeight);
                        """
                    )
                    time.sleep(0.2)
            except Exception:
                pass

        def reveal_profile_element(element, label):
            if not element:
                return
            try:
                print(f"🧭 Cuộn form để lộ {label}...")
                scroll_element_and_ancestors_into_view(driver, element)
            except Exception:
                pass

        if is_chatgpt_home_ready(driver):
            print("✅ Đã vào trang chủ ChatGPT, bỏ qua bước hồ sơ riêng")
            return True

        if _has_chatgpt_home_blocker_text(driver):
            print("🧹 Phát hiện onboarding/chướng ngại vật trước bước hồ sơ, đang dọn...")
            dismiss_chatgpt_onboarding_if_present(driver, max_rounds=8)
            time.sleep(0.8)

        # 1. Nhập họ tên
        print("👤 Đang chờ ô nhập họ tên...")
        name_input = find_profile_name_input_fast(driver, timeout=10)
        if not name_input:
            print("⚠️ Chưa thấy ô họ tên, thử dọn onboarding rồi tìm lại...")
            dismiss_chatgpt_onboarding_if_present(driver, max_rounds=8)
            time.sleep(0.8)
            name_input = find_profile_name_input_fast(driver, timeout=10)
            if not name_input:
                print("⚠️ Vẫn chưa thấy ô họ tên, reload trang hiện tại rồi tìm lại...")
                try:
                    driver.refresh()
                except Exception:
                    pass
                dismiss_chatgpt_onboarding_if_present(driver, max_rounds=6)
                name_input = find_profile_name_input_fast(driver, timeout=10)
                if not name_input:
                    print("❌ Không thấy ô nhập họ tên sau OTP")
                    return False

        robust_fill_input(driver, name_input, user_name, label="họ tên")
        print(f"✅ Đã nhập họ tên: {user_name}")
        set_registered_profile_name(driver, user_name)

        birthdate_input = find_birthdate_input(driver)
        age_input = find_age_input(driver)
        if birthdate_input:
            print("🎂 Phát hiện form nhập ngày sinh dạng dd/mm/yyyy, đang nhập ngày sinh...")
            reveal_profile_element(birthdate_input, "ô ngày sinh")
            if not fill_birthdate_ddmmyyyy_input(driver, birthdate_input, birthdate_text):
                raise Exception(f"Ô ngày sinh không nhận đúng format DD/MM/YYYY: {birthdate_text}")
            print(f"✅ Đã nhập ngày sinh: {birthdate_text}")
        elif age_input:
            profile_age = profile_age_value()
            print("🔞 Phát hiện form nhập tuổi, đang nhập tuổi...")
            reveal_profile_element(age_input, "ô tuổi")
            robust_fill_input(driver, age_input, profile_age, label="tuổi")
            print(f"✅ Đã nhập tuổi: {profile_age}")
        else:
            # 2. Nhập ngày sinh nếu vẫn là form cũ
            print("🎂 Đang nhập ngày sinh...")
            
            # Năm
            year_input = WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[data-type="year"]'))
            )
            reveal_profile_element(year_input, "cụm ngày sinh")
            
            actions = ActionChains(driver)
            actions.click(year_input).perform()
            fill_text_fast(driver, year_input, birthday_year)
            
            # Tháng
            month_input = driver.find_element(By.CSS_SELECTOR, '[data-type="month"]')
            actions = ActionChains(driver)
            actions.click(month_input).perform()
            fill_text_fast(driver, month_input, birthday_month)
            
            # Ngày
            day_input = driver.find_element(By.CSS_SELECTOR, '[data-type="day"]')
            actions = ActionChains(driver)
            actions.click(day_input).perform()
            fill_text_fast(driver, day_input, birthday_day)
            
            print(f"✅ Đã nhập ngày sinh: {birthdate_text}")
        
        accept_profile_agreements_if_present(driver)

        # 3. Click nút tiếp tục cuối cùng
        print("🔘 Click nút gửi cuối cùng...")
        scroll_profile_page_to_bottom_max()
        if not submit_registration_continue(driver, timeout=12):
            raise Exception("Không submit được nút gửi/tiếp tục sau khi điền hồ sơ")
        print("✅ Đã gửi thông tin đăng ký")

        return wait_for_chatgpt_home_ready(driver, timeout=180)
        
    except Exception as e:
        print(f"❌ Điền thông tin thất bại: {e}")
        return False


def handle_stripe_input(driver, field_name, input_selectors, value):
    """
    Điền trường Stripe thông minh
    Logic: tìm trong document chính trước, nếu không thấy thì duyệt đệ quy qua iframe
    """
    selectors = [s.strip() for s in input_selectors.split(',')]
    
    # Hàm phụ: thử tìm và nhập trong ngữ cảnh hiện tại
    def try_fill():
        for selector in selectors:
            try:
                el = driver.find_element(By.CSS_SELECTOR, selector)
                if el.is_displayed():
                    # Cuộn tới vùng nhìn thấy
                    try:
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
                    except:
                        pass
                    type_slowly(el, value)
                    return True
            except:
                continue
        return False

    # 1. Thử document chính
    if try_fill():
        print(f"  ✅ Tìm thấy trong document chính {field_name}")
        return True
        
    # 2. Duyệt đệ quy iframe, hỗ trợ lồng 2 tầng
    def traverse_frames(driver, depth=0, max_depth=2):
        if depth >= max_depth:
            return False
            
        # Lấy tất cả iframe trong ngữ cảnh hiện tại
        frames = driver.find_elements(By.TAG_NAME, "iframe")
        
        for i, frame in enumerate(frames):
            try:
                # Chỉ iframe hiển thị mới có khả năng chứa input
                if not frame.is_displayed():
                    continue
                    
                driver.switch_to.frame(frame)
                
                # Thử điền trong frame hiện tại
                if try_fill():
                    print(f"  ✅ Tìm thấy {field_name} trong iframe (d={depth}, i={i})")
                    driver.switch_to.default_content() # Sau khi tìm thấy thì reset về document chính
                    return True
                
                # Tìm đệ quy frame con
                if traverse_frames(driver, depth + 1, max_depth):
                    return True
                    
                # Quay về frame cha
                driver.switch_to.parent_frame()
                
            except Exception as e:
                # Có exception, thử quay lại rồi tiếp tục
                try: driver.switch_to.parent_frame()
                except: pass
                continue
        
        return False

    driver.switch_to.default_content()
    if traverse_frames(driver):
        return True
                
    print(f"  ❌ Không tìm thấy {field_name}")
    return False


def subscribe_plus_trial(driver):
    """
    Đăng ký dùng thử miễn phí ChatGPT Plus, bản địa chỉ Nhật Bản
    """
    print("\n" + "=" * 50)
    print("💳 Bắt đầu quy trình đăng ký dùng thử Plus")
    print("   Sẽ tự phát hiện quốc gia trên trang và tạo địa chỉ tương ứng")
    print("=" * 50)
    
    wait = WebDriverWait(driver, 30)
    
    try:
        # 1. Truy cập trang Pricing
        url = "https://chatgpt.com/?promo_campaign=plus-1-month-free#pricing"

        def is_pricing_ready():
            try:
                return bool(driver.execute_script(
                    """
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                    const lower = s => norm(s).toLowerCase();
                    const hasPricingSwitch = [...document.querySelectorAll('[role="group"]')].some(el => {
                        const haystack = `${el.getAttribute('aria-label') || ''} ${el.innerText || ''}`.toLowerCase();
                        return (haystack.includes('cá nhân') && haystack.includes('doanh nghiệp'))
                            || (haystack.includes('personal') && (haystack.includes('business') || haystack.includes('enterprise')));
                    });
                    const visibleCards = [...document.querySelectorAll('[data-testid$="pricing-modal-column"], [data-pricing-column-content], #plus-pricing, #free-pricing, #go-pricing, #pro-pricing')]
                        .map(el => ({el, text: lower(el.innerText || el.textContent || ''), r: el.getBoundingClientRect()}))
                        .filter(x => x.r.width > 120 && x.r.height > 180 && x.r.bottom > 0 && x.r.top < window.innerHeight);
                    const planNames = new Set();
                    for (const card of visibleCards) {
                        if (card.text.includes('free')) planNames.add('free');
                        if (card.text.includes('go')) planNames.add('go');
                        if (card.text.includes('plus')) planNames.add('plus');
                        if (card.text.includes('pro')) planNames.add('pro');
                    }
                    const plusCard = visibleCards.find(card => card.text.includes('plus'));
                    const hasPromoButton = visibleCards.some(card =>
                        card.text.includes('nhận ưu đãi miễn phí')
                        || card.text.includes('try for free')
                        || card.text.includes('free trial')
                    );
                    return hasPricingSwitch && planNames.size >= 2 && !!plusCard && hasPromoButton;
                    """
                ))
            except Exception:
                return False

        def has_visible_pricing_plans():
            try:
                return bool(driver.execute_script(
                    """
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const visibleCards = [...document.querySelectorAll('[data-testid$="pricing-modal-column"], [data-pricing-column-content], #plus-pricing, #free-pricing, #go-pricing, #pro-pricing')]
                        .map(el => ({el, text: norm(el.innerText || el.textContent || ''), r: el.getBoundingClientRect()}))
                        .filter(x => x.r.width > 120 && x.r.height > 180 && x.r.bottom > 0 && x.r.top < window.innerHeight);
                    return visibleCards.some(card => card.text.includes('plus'))
                        && visibleCards.length >= 2;
                    """
                ))
            except Exception:
                return False

        def pricing_blocker_state():
            try:
                return driver.execute_script(
                    """
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                    const url = location.href;
                    const text = norm(document.body.innerText || '').toLowerCase();
                    if (text.includes('điều gì thôi thúc bạn sử dụng chatgpt') || text.includes('what brings you to chatgpt')) return 'motivation_onboarding';
                    if (text.includes('đoạn chat ví dụ') || text.includes('hỏi gì cũng được') || text.includes('bỏ qua tìm hiểu') || text.includes('example chat') || text.includes('ask anything')) return 'tips_onboarding';
                    if (text.includes('bạn đã hoàn tất') || text.includes('you are all set')) return 'all_set_onboarding';
                    if (text.includes('lời khuyên để bắt đầu') || text.includes('tips for getting started')) return 'tips_onboarding';
                    if (url.includes('/auth/') || url.includes('auth.openai.com')) return 'auth_flow';
                    if (url.includes('/c/') || text.includes('đoạn chat mới')) return 'chat_home';
                    if (url.includes('promo_campaign=plus-1-month-free') || url.includes('#pricing')) return 'pricing_loading';
                    return url;
                    """
                ) or ""
            except Exception:
                return ""

        def has_pricing_hard_error():
            try:
                text = (driver.execute_script("return document.body.innerText || ''") or "").lower()
            except Exception:
                text = ""
            hard_error_keywords = (
                "something went wrong",
                "try again",
                "timed out",
                "operation timeout",
                "route error",
                "invalid content",
                "đã xảy ra lỗi",
                "thử lại",
            )
            return any(keyword in text for keyword in hard_error_keywords)

        def find_free_offer_button():
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            try:
                return driver.execute_script(
                    """
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                    const lower = s => norm(s).toLowerCase();
                    const selectors = ['button.button-glimmer-cta', 'button', '[role="button"]', 'a'];
                    const seen = new Set();
                    const candidates = selectors.flatMap(sel => [...document.querySelectorAll(sel)])
                        .filter(el => {
                            if (seen.has(el)) return false;
                            seen.add(el);
                            const r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0 || r.bottom < 0 || r.top > window.innerHeight) return false;
                            const text = lower(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                            if (!text) return false;
                            if (text.includes('nhận ưu đãi miễn phí')) return false;
                            if (text.includes('nâng cấp')) return false;
                            return text === 'ưu đãi miễn phí'
                                || text.includes('ưu đãi miễn phí')
                                || text === 'free offer'
                                || text.includes('free offer');
                        })
                        .map(el => ({el, text: norm(el.innerText || el.textContent || el.getAttribute('aria-label') || ''), r: el.getBoundingClientRect()}))
                        .sort((a, b) => {
                            const aGlimmer = a.el.classList.contains('button-glimmer-cta') ? 0 : 1;
                            const bGlimmer = b.el.classList.contains('button-glimmer-cta') ? 0 : 1;
                            return aGlimmer - bGlimmer || a.r.top - b.r.top;
                        });
                    const target = candidates[0];
                    if (!target) return null;
                    target.el.scrollIntoView({block: 'center', inline: 'center'});
                    return target.el;
                    """
                )
            except Exception as e:
                print(f"⚠️ Tìm nút Ưu đãi miễn phí lỗi: {e}")
            return None

        def has_free_offer_button_visible():
            try:
                return bool(find_free_offer_button())
            except Exception:
                return False

        def wait_until_home_promo_stable(timeout=4.0, stable_for=1.2):
            stable_since = None
            deadline = time.time() + timeout
            while time.time() < deadline:
                visible = has_free_offer_button_visible() and not _has_chatgpt_home_blocker_text(driver)
                if visible:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= stable_for:
                        return True
                else:
                    stable_since = None
                time.sleep(0.12)
            return False

        def clear_blockers_before_free_offer_click():
            """Dọn blocker trước, rồi chờ trang ổn định 4s. Sau đó không clear nữa trước khi bấm promo."""
            try:
                for _ in range(6):
                    if not _has_chatgpt_home_blocker_text(driver):
                        break
                    dismissed = dismiss_chatgpt_onboarding_if_present(driver, max_rounds=2)
                    try:
                        driver.execute_script(
                            """
                            const sel = window.getSelection && window.getSelection();
                            if (sel) sel.removeAllRanges();
                            document.activeElement && document.activeElement.blur && document.activeElement.blur();
                            """
                        )
                    except Exception:
                        pass
                    time.sleep(0.45 if dismissed else 0.25)
                print("  ⏳ Đợi trang chủ ổn định 4s trước khi bấm Ưu đãi miễn phí...")
                time.sleep(4.0)
            except Exception:
                pass

        def click_free_offer_button():
            """Double-click CTA Ưu đãi miễn phí giống nút auth, rồi mới fallback sang URL."""
            try:
                print("  🧹 Dọn vật cản trước khi bấm Ưu đãi miễn phí...")
                clear_blockers_before_free_offer_click()
                time.sleep(0.25)

                target = find_free_offer_button()
                if not target:
                    return False
                text = (target.text or target.get_attribute("aria-label") or "").strip()
                if double_click_until_auth_page_changes(
                    driver,
                    find_free_offer_button,
                    is_pricing_ready,
                    label="ưu đãi miễn phí",
                    timeout=10,
                    interval=0.3,
                ):
                    print(f"🧭 Đã double-click nút UI: {text or 'Ưu đãi miễn phí'}")
                    return True
                after_url = ""
                try:
                    after_url = driver.current_url
                except Exception:
                    pass
                print(f"⚠️ PROMO_CLICK_STAYED_HOME: double-click promo chưa mở pricing, url={after_url}")
                return "STAYED_HOME"
            except Exception as e:
                print(f"⚠️ Click nút Ưu đãi miễn phí lỗi: {e}")
            return False

        def has_plus_without_trial_button():
            """Sau onboarding, nếu chỉ còn CTA Dùng bản Plus thì account không có trial."""
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            try:
                return bool(driver.execute_script(
                    """
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                    const lower = s => norm(s).toLowerCase();
                    const candidates = [...document.querySelectorAll('button.button-glimmer-cta,button,[role="button"],a')]
                        .map(el => ({el, text: lower(el.innerText || el.textContent || el.getAttribute('aria-label') || ''), r: el.getBoundingClientRect()}))
                        .filter(x => x.r.width > 0 && x.r.height > 0 && x.r.bottom > 0 && x.r.top < window.innerHeight);
                    const hasFreeOffer = candidates.some(x =>
                        (x.text === 'ưu đãi miễn phí' || x.text.includes('ưu đãi miễn phí') || x.text === 'free offer' || x.text.includes('free offer'))
                        && !x.text.includes('nhận ưu đãi miễn phí')
                    );
                    const hasUsePlus = candidates.some(x =>
                        x.text === 'dùng bản plus'
                        || x.text.includes('dùng bản plus')
                        || x.text === 'try plus'
                        || x.text.includes('try plus')
                        || x.text === 'use plus'
                        || x.text.includes('use plus')
                    );
                    return hasUsePlus && !hasFreeOffer;
                    """
                ))
            except Exception:
                return False

        def navigate_to_pricing(new_tab=False):
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            if not has_free_offer_button_visible():
                for _ in range(2):
                    dismissed = dismiss_chatgpt_onboarding_if_present(driver, max_rounds=4)
                    if not dismissed or has_free_offer_button_visible():
                        break
                    time.sleep(0.4)
            if new_tab:
                try:
                    before = set(driver.window_handles)
                    driver.execute_script("window.open(arguments[0], '_blank');", url)
                    time.sleep(0.8)
                    after = [h for h in driver.window_handles if h not in before]
                    driver.switch_to.window(after[-1] if after else driver.window_handles[-1])
                    apply_zoom_after_tab_switch(driver, zoom_factor=1.0)
                    print("🧭 Đã mở tab mới trong cùng profile để vào pricing")
                    return True
                except Exception as e:
                    print(f"⚠️ Mở tab mới pricing lỗi, fallback tab hiện tại: {e}")
            try:
                driver.execute_script(
                    """
                    const sel = window.getSelection && window.getSelection();
                    if (sel) sel.removeAllRanges();
                    document.activeElement && document.activeElement.blur && document.activeElement.blur();
                    window.location.assign(arguments[0]);
                    """,
                    url,
                )
            except Exception:
                driver.get(url)
            return True

        def scroll_pricing_page_to_country_section():
            """Cuộn pricing xuống đáy thật nhanh để lộ country picker."""
            print("  🖱️ Thực thi cuộn pricing xuống đáy...")
            try:
                target_debug = driver.execute_script(
                    """
                    const uniq = arr => [...new Set(arr.filter(Boolean))];
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const findCountryPicker = () => {
                        const labels = [...document.querySelectorAll('[id]')].filter(el => {
                            const t = norm(el.innerText || el.textContent || '');
                            return t === 'quốc gia và tiền tệ'
                                || t === 'country and currency'
                                || (t.includes('quốc gia') && t.includes('tiền tệ'))
                                || (t.includes('country') && t.includes('currency'));
                        });
                        const byLabel = [];
                        for (const label of labels) {
                            byLabel.push(...document.querySelectorAll(`button[role="combobox"][aria-labelledby~="${CSS.escape(label.id)}"]`));
                        }
                        const byText = [...document.querySelectorAll('button[role="combobox"]')].filter(el => {
                            const t = norm(el.innerText || el.textContent || '');
                            return t.includes('hàn quốc') || t.includes('korea') || t.includes('indonesia')
                                || t.includes('việt nam') || t.includes('vietnam') || t.includes('united states') || t.includes('mỹ');
                        });
                        return [...new Set([...byLabel, ...byText])][0] || null;
                    };
                    const targets = uniq([
                        document.querySelector('#modal-account-payment'),
                        ...document.querySelectorAll('#modal-account-payment *'),
                        ...document.querySelectorAll('dialog[open], dialog'),
                        document.querySelector('[role="dialog"]'),
                        ...document.querySelectorAll('[role="dialog"] *'),
                        document.scrollingElement || document.documentElement,
                        document.body,
                        ...document.querySelectorAll('main, [role="main"], [data-testid*="pricing"], [class*="pricing"], [class*="overflow"], [style*="overflow"], [data-radix-popper-content-wrapper]')
                    ]);
                    document.activeElement && document.activeElement.blur && document.activeElement.blur();
                    if (document.body) document.body.focus();
                    const picker = findCountryPicker();
                    if (picker) {
                        let node = picker;
                        while (node) {
                            try {
                                if (node.scrollHeight > node.clientHeight + 20) {
                                    node.scrollTop = node.scrollHeight;
                                }
                            } catch (e) {}
                            node = node.parentElement;
                        }
                        try { picker.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
                    }
                    const snapshots = [];
                    for (const el of targets) {
                        try {
                            const r = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            const scrollable = el.scrollHeight > el.clientHeight + 20;
                            snapshots.push({
                                tag: el.tagName,
                                id: el.id || '',
                                cls: (el.className || '').toString().slice(0, 120),
                                top: Math.round(r.top),
                                height: Math.round(r.height),
                                scrollTop: el.scrollTop || 0,
                                scrollHeight: el.scrollHeight || 0,
                                clientHeight: el.clientHeight || 0,
                                overflowY: style.overflowY || '',
                                scrollable,
                            });
                            if (scrollable) {
                                el.scrollTop = el.scrollHeight;
                            }
                        } catch (e) {}
                    }
                    window.scrollTo(0, document.body.scrollHeight);
                    return snapshots
                        .filter(x => x.scrollable)
                        .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))
                        .slice(0, 5)
                        .map(x => ({
                            ...x,
                            pickerFound: !!picker,
                            pickerText: picker ? (picker.innerText || picker.textContent || '').trim() : '',
                        }));
                    """
                )
                print(f"  🧭 Top container cuộn: {target_debug or 'KHÔNG TÌM THẤY CONTAINER SCROLLABLE'}")
                time.sleep(0.4)
                try:
                    top_after_first = driver.execute_script("return window.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop || 0;")
                    print(f"  📍 Vị trí scroll sau nhịp 1: {top_after_first}")
                except Exception:
                    pass
                driver.execute_script(
                    """
                    const targets = [
                        document.querySelector('#modal-account-payment'),
                        ...document.querySelectorAll('#modal-account-payment *'),
                        ...document.querySelectorAll('dialog[open], dialog'),
                        document.scrollingElement || document.documentElement,
                        document.body,
                        ...document.querySelectorAll('main, [role="main"], [data-testid*="pricing"], [class*="pricing"], [class*="overflow"], [style*="overflow"], [data-radix-popper-content-wrapper]')
                    ];
                    for (const el of targets) {
                        if (!el) continue;
                        try {
                            if (el.scrollHeight > el.clientHeight + 20) {
                                el.scrollTop = el.scrollHeight;
                            }
                        } catch (e) {}
                    }
                    window.scrollTo(0, document.body.scrollHeight);
                    """
                )
                try:
                    body = driver.find_element(By.TAG_NAME, "body")
                    body.send_keys(Keys.END)
                    time.sleep(0.2)
                    body.send_keys(Keys.END)
                except Exception:
                    pass
                try:
                    ActionChains(driver).send_keys(Keys.END).perform()
                    time.sleep(0.15)
                    ActionChains(driver).send_keys(Keys.END).perform()
                except Exception:
                    pass
                try:
                    driver.execute_script(
                        """
                        const wheel = delta => window.dispatchEvent(new WheelEvent('wheel', {deltaY: delta, bubbles: true, cancelable: true}));
                        wheel(2500);
                        wheel(2500);
                        wheel(2500);
                        """
                    )
                except Exception:
                    pass
                try:
                    top_after_final = driver.execute_script("return window.pageYOffset || document.documentElement.scrollTop || document.body.scrollTop || 0;")
                    print(f"  ✅ Vị trí scroll sau cuộn đáy: {top_after_final}")
                except Exception:
                    pass
            except Exception:
                try:
                    ActionChains(driver).scroll_by_amount(0, 5000).perform()
                    time.sleep(0.2)
                    ActionChains(driver).scroll_by_amount(0, 5000).perform()
                except Exception:
                    pass

        def open_pricing_and_wait(max_attempts=3):
            for attempt in range(max_attempts):
                use_new_tab = attempt == max_attempts - 1
                print(f"🌐 Đã dọn vật cản, đang mở {url}... (lần {attempt + 1}/{max_attempts}{', tab mới' if use_new_tab else ''})")
                nav_result = navigate_to_pricing(new_tab=use_new_tab)

                deadline = time.time() + 90
                last_state = ""
                same_state_since = time.time()
                last_url = ""
                plans_seen_once = False
                force_scroll_after = time.time() + 5
                forced_scroll_done = False
                while time.time() < deadline:
                    current_url = ""
                    try:
                        current_url = driver.current_url
                    except Exception:
                        pass

                    state = pricing_blocker_state()
                    if state != last_state or current_url != last_url:
                        print(f"  ⏳ Pricing chưa sẵn sàng, trạng thái hiện tại: {state}")
                        last_state = state
                        last_url = current_url
                        same_state_since = time.time()
                    if not forced_scroll_done and time.time() >= force_scroll_after:
                        forced_scroll_done = True
                        print("  🖱️ Đã vào pricing được 5s, cuộn thẳng xuống đáy trang...")
                        try:
                            scroll_pricing_page_to_country_section()
                        except Exception as e:
                            print(f"  ⚠️ Cuộn đáy pricing lỗi: {e}")
                    if has_visible_pricing_plans() and not plans_seen_once:
                        plans_seen_once = True
                        print("  👀 Đã thấy plan pricing, cuộn xuống ngay để lộ phần đổi quốc gia...")
                        try:
                            scroll_pricing_page_to_country_section()
                        except Exception:
                            pass
                        time.sleep(0.8)
                        print("  ✅ Đã thấy plan pricing, chuyển sang bước chọn quốc gia ngay")
                        return True
                    if forced_scroll_done and ("promo_campaign=plus-1-month-free" in current_url or "#pricing" in current_url):
                        if time.time() - force_scroll_after >= 3:
                            print("  ✅ Đã ở đúng URL pricing đủ lâu, chuyển sang bước chọn quốc gia ngay")
                            return True
                    if is_pricing_ready():
                        print("✅ Trang pricing đã load đủ các gói")
                        return True

                    if has_pricing_hard_error():
                        print("⚠️ Pricing gặp lỗi hiển thị rõ ràng, sẽ mở lại URL pricing")
                        break

                    # Khi đã vào pricing/loading thì chỉ chờ render, không click bừa popup nữa.
                    if state in ("pricing_loading", "loading"):
                        time.sleep(0.5)
                        continue

                    # Nếu bị rơi lại onboarding/home, thoát vòng chờ để dọn sạch rồi mở link lại từ đầu.
                    if state in ("motivation_onboarding", "all_set_onboarding", "tips_onboarding", "chat_home"):
                        if time.time() - same_state_since >= 3:
                            print(f"⚠️ Bị trả về trạng thái {state}, sẽ dọn vật cản rồi mở lại pricing")
                            break
                        time.sleep(0.5)
                        continue

                    # Chỉ coi là kẹt khi trạng thái + URL đứng yên khá lâu.
                    if time.time() - same_state_since >= 25:
                        print(f"⚠️ Pricing đứng yên quá lâu ở trạng thái {state}, sẽ mở lại URL pricing")
                        break

                    time.sleep(0.5)

                print("⚠️ Pricing chưa load đủ sau khi chờ dài hơn, mở lại URL pricing theo trạng thái hiện tại")
                nav_result = navigate_to_pricing(new_tab=False)
                time.sleep(2)
            return False

        pricing_open_result = open_pricing_and_wait(max_attempts=4)
        if not pricing_open_result:
            print("❌ Không mở được trang pricing sau nhiều lần chờ/reload")
            return False
        
        # 2. Click nút đăng ký Plus, đảm bảo chọn Plus thay vì Team
        print("🔘 Tìm nút đăng ký Plus...")
        subscribe_btn = None

        def select_personal_pricing_tab():
            try:
                try:
                    driver.execute_script("window.scrollTo(0, 0);")
                    time.sleep(0.4)
                except Exception:
                    pass
                clicked = driver.execute_script(
                    """
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                    const groups = [...document.querySelectorAll('[role="group"]')]
                        .map(el => ({el, label: norm(el.getAttribute('aria-label') || ''), text: norm(el.innerText || el.textContent || ''), r: el.getBoundingClientRect()}))
                        .filter(x => x.r.width > 0 && x.r.height > 0 && x.r.top < window.innerHeight * 0.5);
                    const group = groups.find(x => {
                        const haystack = `${x.label} ${x.text}`.toLowerCase();
                        return (haystack.includes('cá nhân') && haystack.includes('doanh nghiệp'))
                            || (haystack.includes('personal') && (haystack.includes('business') || haystack.includes('enterprise')));
                    });
                    if (!group) return '';

                    const personal = [...group.el.querySelectorAll('button[role="radio"],button')]
                        .map(el => ({el, text: norm(el.innerText || el.textContent || ''), label: norm(el.getAttribute('aria-label') || ''), r: el.getBoundingClientRect()}))
                        .find(x => {
                            const haystack = `${x.text} ${x.label}`.toLowerCase();
                            return haystack.includes('cá nhân') || haystack.includes('personal');
                        });
                    if (!personal || personal.r.width <= 0 || personal.r.height <= 0) return '';

                    const business = [...group.el.querySelectorAll('button[role="radio"],button')]
                        .map(el => ({el, text: norm(el.innerText || el.textContent || ''), label: norm(el.getAttribute('aria-label') || ''), checked: el.getAttribute('aria-checked'), state: el.getAttribute('data-state')}))
                        .find(x => {
                            const haystack = `${x.text} ${x.label}`.toLowerCase();
                            return haystack.includes('doanh nghiệp') || haystack.includes('business') || haystack.includes('enterprise');
                        });
                    const personalAlreadyOn = personal.el.getAttribute('aria-checked') === 'true'
                        || personal.el.getAttribute('data-state') === 'on';
                    const businessOn = business && (business.checked === 'true' || business.state === 'on');
                    if (personalAlreadyOn && !businessOn) {
                        return personal.text || personal.label || 'Cá nhân';
                    }

                    personal.el.scrollIntoView({block: 'center', inline: 'center'});
                    personal.el.click();
                    return personal.text || personal.label || 'Cá nhân';
                    """
                )
                if clicked:
                    print(f"  -> Đã chọn tab cá nhân: {clicked}")
                    time.sleep(0.8)
                    return True
            except Exception as e:
                print(f"  ⚠️ JS chọn tab cá nhân lỗi: {e}")

            personal_xpaths = [
                '//*[@role="group" and (contains(@aria-label, "Cá nhân") or contains(@aria-label, "Personal")) and (contains(@aria-label, "Doanh nghiệp") or contains(@aria-label, "Business"))]//button[@role="radio" and (contains(@aria-label, "Cá nhân") or contains(normalize-space(.), "Cá nhân") or contains(@aria-label, "Personal") or contains(normalize-space(.), "Personal"))]',
                '//button[@role="radio" and @aria-checked and (contains(@aria-label, "Cá nhân") or normalize-space(.)="Cá nhân" or contains(@aria-label, "Personal") or normalize-space(.)="Personal")]',
            ]
            for xpath in personal_xpaths:
                try:
                    for tab in driver.find_elements(By.XPATH, xpath):
                        if not tab.is_displayed() or not tab.is_enabled():
                            continue
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();", tab)
                        print(f"  -> Đã click tab cá nhân: {tab.text.strip() or tab.get_attribute('aria-label')}")
                        time.sleep(0.8)
                        return True
                except Exception:
                    continue
            return False

        def is_business_pricing_active():
            try:
                return bool(driver.execute_script(
                    """
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                    const groups = [...document.querySelectorAll('[role="group"]')]
                        .map(el => ({el, label: norm(el.getAttribute('aria-label') || ''), text: norm(el.innerText || el.textContent || ''), r: el.getBoundingClientRect()}))
                        .filter(x => x.r.width > 0 && x.r.height > 0 && x.r.top < window.innerHeight * 0.5);
                    const group = groups.find(x => {
                        const haystack = `${x.label} ${x.text}`.toLowerCase();
                        return (haystack.includes('cá nhân') && haystack.includes('doanh nghiệp'))
                            || (haystack.includes('personal') && (haystack.includes('business') || haystack.includes('enterprise')));
                    });
                    if (group) {
                        const buttons = [...group.el.querySelectorAll('button[role="radio"],button')];
                        for (const btn of buttons) {
                            const text = `${norm(btn.innerText || btn.textContent || '')} ${norm(btn.getAttribute('aria-label') || '')}`.toLowerCase();
                            const active = btn.getAttribute('aria-checked') === 'true' || btn.getAttribute('data-state') === 'on';
                            if (active && (text.includes('doanh nghiệp') || text.includes('business') || text.includes('enterprise'))) {
                                return true;
                            }
                        }
                        return false;
                    }
                    const body = norm(document.body.innerText || '').toLowerCase();
                    return body.includes('chatgpt doanh nghiệp') && !body.includes('nhận ưu đãi miễn phí');
                    """
                ))
            except Exception:
                return False

        def ensure_personal_pricing_tab():
            for _ in range(3):
                if select_personal_pricing_tab():
                    if not is_business_pricing_active():
                        return True
                time.sleep(0.6)
            return not is_business_pricing_active()

        def open_country_selector_after_scroll():
            """Fallback mạnh: cuộn xong thì mở country selector bằng nhiều selector rộng."""
            print("  🌏 Fallback mở country selector sau khi cuộn trang...")
            scroll_pricing_page_to_country_section()
            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.7);")
                time.sleep(2.5)
            except Exception:
                pass

            selector_specs = [
                ("xpath", "//span[normalize-space()='Quốc gia và tiền tệ']/ancestor::*[self::button or self::div]"),
                ("xpath", "//span[normalize-space()='Country and currency']/ancestor::*[self::button or self::div]"),
                ("xpath", "//button[@role='combobox'][.//span[normalize-space()='Hàn Quốc' or normalize-space()='Korea' or normalize-space()='Indonesia' or normalize-space()='Việt Nam' or normalize-space()='Vietnam' or normalize-space()='United States' or normalize-space()='Mỹ']]"),
                ("xpath", "//button[contains(., 'Country') or contains(., 'Change country') or contains(., 'Region') or contains(., 'Quốc gia')]"),
                ("xpath", "//div[contains(., 'Country') or contains(., 'Change country') or contains(., 'Quốc gia và tiền tệ')]"),
                ("css", "button[data-testid*='country']"),
                ("xpath", "//button[contains(@class, 'country')]"),
                ("xpath", "//button[@role='combobox'][contains(., 'Hàn Quốc') or contains(., 'Korea') or contains(., 'Indonesia') or contains(., 'Việt Nam') or contains(., 'Vietnam') or contains(., 'Mỹ') or contains(., 'United States')]"),
            ]

            for kind, selector in selector_specs:
                try:
                    locator = (By.XPATH, selector) if kind == "xpath" else (By.CSS_SELECTOR, selector)
                    element = WebDriverWait(driver, 5).until(EC.element_to_be_clickable(locator))
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
                    time.sleep(1.0)
                    try:
                        driver.execute_script("arguments[0].click();", element)
                    except Exception:
                        try:
                            element.click()
                        except Exception:
                            ActionChains(driver).move_to_element(element).click().perform()
                    print("  ✅ Đã mở Country selector bằng fallback selector")
                    try:
                        window_list_id = element.get_attribute("aria-controls") or ""
                    except Exception:
                        window_list_id = ""
                    try:
                        driver.execute_script(
                            """
                            window.__pricingCountryCombobox = arguments[0];
                            window.__pricingCountryListId = arguments[1] || '';
                            """,
                            element,
                            window_list_id,
                        )
                    except Exception:
                        pass
                    return True
                except Exception:
                    continue
            return False

        def scroll_to_pricing_country_picker():
            """Đưa country picker cuối trang vào giữa màn hình bằng label/combobox thật."""
            try:
                scroll_pricing_page_to_country_section()
                for _ in range(12):
                    picker_text = driver.execute_script(
                        """
                        const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                        const roots = [document];
                        const findPicker = () => {
                            const labels = [...document.querySelectorAll('[id]')]
                                .filter(el => {
                                    const t = norm(el.innerText || el.textContent || '').toLowerCase();
                                    return t === 'quốc gia và tiền tệ'
                                        || t === 'country and currency'
                                        || t.includes('quốc gia') && t.includes('tiền tệ')
                                        || t.includes('country') && t.includes('currency');
                                });
                            const byLabel = [];
                            for (const label of labels) {
                                byLabel.push(...document.querySelectorAll(`button[role="combobox"][aria-labelledby~="${CSS.escape(label.id)}"]`));
                            }
                            const countryWords = ['hàn quốc', 'korea', 'south korea', 'kr', 'united states', 'mỹ', 'vietnam', 'việt nam', 'indonesia'];
                            const fallback = [...document.querySelectorAll('button[role="combobox"]')]
                                .filter(el => {
                                    const text = norm(el.innerText || el.textContent || '').toLowerCase();
                                    const r = el.getBoundingClientRect();
                                    return r.width > 0 && r.height > 0
                                        && countryWords.some(w => text === w || text.includes(w) || text.startsWith(w));
                                });
                            return [...new Set([...byLabel, ...fallback])]
                                .map(el => ({el, text: norm(el.innerText || el.textContent || ''), r: el.getBoundingClientRect()}))
                                .filter(x => x.r.width > 0 && x.r.height > 0)
                                .sort((a, b) => (a.r.top - b.r.top) || (a.r.left - b.r.left))
                                .pop() || null;
                        };

                        const picker = findPicker();
                        if (picker) {
                            let node = picker.el;
                            while (node) {
                                try {
                                    if (node.scrollHeight > node.clientHeight + 20) {
                                        node.scrollTop = node.scrollHeight;
                                    }
                                } catch (e) {}
                                node = node.parentElement;
                            }
                            picker.el.scrollIntoView({block: 'center', inline: 'center'});
                            window.__pricingCountryCombobox = picker.el;
                            window.__pricingCountryListId = picker.el.getAttribute('aria-controls') || '';
                            return picker.text;
                        }

                        const containers = [
                            document.querySelector('#modal-account-payment'),
                            ...document.querySelectorAll('#modal-account-payment *'),
                            ...document.querySelectorAll('dialog[open], dialog'),
                            document.scrollingElement || document.documentElement,
                            ...document.querySelectorAll('main, [role="main"], [data-testid*="pricing"], [class*="pricing"], [class*="overflow"], [style*="overflow"], [data-radix-popper-content-wrapper]')
                        ].filter(Boolean);
                        for (const el of containers) {
                            if (!el) continue;
                            try {
                                if (el.scrollHeight > el.clientHeight + 20) {
                                    el.scrollTop = Math.min(el.scrollTop + Math.max(500, Math.floor(el.clientHeight * 0.85)), el.scrollHeight);
                                }
                            } catch (e) {}
                        }
                        window.scrollBy(0, Math.max(500, Math.floor(window.innerHeight * 0.85)));
                        return '';
                        """
                    ) or ""
                    if picker_text:
                        return picker_text
                    time.sleep(0.35)
                return ""
            except Exception:
                return ""

        def click_current_country_picker():
            try:
                clicked = driver.execute_script(
                    """
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                    let picker = window.__pricingCountryCombobox;
                    if (!picker || !document.contains(picker)) {
                        const roots = [
                            document.querySelector('#modal-account-payment'),
                            ...document.querySelectorAll('dialog[open], dialog'),
                            document,
                        ].filter(Boolean);
                        const labels = [...roots.flatMap(root => [...(root.querySelectorAll ? root.querySelectorAll('[id]') : [])])]
                            .filter(el => {
                                const t = norm(el.innerText || el.textContent || '').toLowerCase();
                                return t === 'quốc gia và tiền tệ'
                                    || t === 'country and currency'
                                    || t.includes('quốc gia') && t.includes('tiền tệ')
                                    || t.includes('country') && t.includes('currency');
                            });
                        const found = [];
                        for (const label of labels) {
                            for (const root of roots) {
                                found.push(...(root.querySelectorAll ? root.querySelectorAll(`button[role="combobox"][aria-labelledby~="${CSS.escape(label.id)}"]`) : []));
                            }
                        }
                        const byText = [...roots.flatMap(root => [...(root.querySelectorAll ? root.querySelectorAll('button[role="combobox"]') : [])])]
                            .find(el => {
                                const text = norm(el.innerText || el.textContent || '').toLowerCase();
                                const r = el.getBoundingClientRect();
                                return r.width > 0 && r.height > 0 && (
                                    text.includes('hàn quốc') || text.includes('korea') || text.includes('indonesia')
                                    || text.includes('việt nam') || text.includes('vietnam') || text.includes('united states') || text.includes('mỹ')
                                );
                            });
                        picker = found.find(el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        }) || byText || null;
                    }
                    if (!picker) return {text: '', opened: false};
                    const text = norm(picker.innerText || picker.textContent || '');
                    picker.scrollIntoView({block: 'center', inline: 'center'});
                    try { picker.click(); } catch (e) {}
                    window.__pricingCountryCombobox = picker;
                    window.__pricingCountryListId = picker.getAttribute('aria-controls') || '';
                    const listId = window.__pricingCountryListId || '';
                    const controlled = listId ? document.getElementById(listId) : null;
                    const opened = picker.getAttribute('aria-expanded') === 'true'
                        || !!(controlled && controlled.getBoundingClientRect().height > 0);
                    return {text, opened};
                    """
                ) or {}
                if clicked and clicked.get("text"):
                    if clicked.get("opened"):
                        return clicked.get("text") or ""
            except Exception:
                pass
            try:
                picker_candidates = driver.find_elements(
                    By.XPATH,
                    "//button[@role='combobox'][.//span[normalize-space()='Hàn Quốc' or normalize-space()='Korea' or normalize-space()='Indonesia' or normalize-space()='Việt Nam' or normalize-space()='Vietnam' or normalize-space()='United States' or normalize-space()='Mỹ'] or contains(., 'Hàn Quốc') or contains(., 'Korea') or contains(., 'Indonesia') or contains(., 'Việt Nam') or contains(., 'Vietnam') or contains(., 'United States') or contains(., 'Mỹ')]"
                )
                for picker in picker_candidates:
                    if not picker.is_displayed():
                        continue
                    text = (picker.text or picker.get_attribute("aria-label") or "").strip()
                    try:
                        driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", picker)
                    except Exception:
                        pass
                    strategies = [
                        ("js", lambda: driver.execute_script("arguments[0].click();", picker)),
                        ("native", lambda: picker.click()),
                        ("actions", lambda: ActionChains(driver).move_to_element(picker).pause(0.1).click().perform()),
                    ]
                    rect = None
                    try:
                        rect = driver.execute_script(
                            """
                            const r = arguments[0].getBoundingClientRect();
                            return {
                                left: r.left,
                                top: r.top,
                                width: r.width,
                                height: r.height,
                                cx: r.left + (r.width / 2),
                                cy: r.top + (r.height / 2),
                            };
                            """,
                            picker,
                        )
                    except Exception:
                        rect = None
                    arrow_rect = None
                    try:
                        arrow_rect = driver.execute_script(
                            """
                            const picker = arguments[0];
                            const icon = picker.querySelector('svg.icon-sm, svg');
                            if (!icon) return null;
                            const r = icon.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) return null;
                            return {
                                left: r.left,
                                top: r.top,
                                width: r.width,
                                height: r.height,
                                cx: r.left + (r.width / 2),
                                cy: r.top + (r.height / 2),
                            };
                            """,
                            picker,
                        )
                    except Exception:
                        arrow_rect = None

                    if arrow_rect and arrow_rect.get("width", 0) > 0 and arrow_rect.get("height", 0) > 0:
                        strategies.append((
                            "cdp-svg-arrow",
                            lambda: (
                                driver.execute_cdp_cmd(
                                    "Input.dispatchMouseEvent",
                                    {"type": "mouseMoved", "x": arrow_rect["cx"], "y": arrow_rect["cy"], "button": "left", "buttons": 1},
                                ),
                                driver.execute_cdp_cmd(
                                    "Input.dispatchMouseEvent",
                                    {"type": "mousePressed", "x": arrow_rect["cx"], "y": arrow_rect["cy"], "button": "left", "buttons": 1, "clickCount": 1},
                                ),
                                driver.execute_cdp_cmd(
                                    "Input.dispatchMouseEvent",
                                    {"type": "mouseReleased", "x": arrow_rect["cx"], "y": arrow_rect["cy"], "button": "left", "buttons": 1, "clickCount": 1},
                                ),
                            ),
                        ))
                    if rect and rect.get("width", 0) > 0 and rect.get("height", 0) > 0:
                        arrow_x = rect["left"] + max(rect["width"] * 0.86, rect["width"] - 18)
                        arrow_y = rect["top"] + (rect["height"] / 2)
                        strategies.append((
                            "cdp-arrow",
                            lambda: (
                                driver.execute_cdp_cmd(
                                    "Input.dispatchMouseEvent",
                                    {"type": "mouseMoved", "x": arrow_x, "y": arrow_y, "button": "left", "buttons": 1},
                                ),
                                driver.execute_cdp_cmd(
                                    "Input.dispatchMouseEvent",
                                    {"type": "mousePressed", "x": arrow_x, "y": arrow_y, "button": "left", "buttons": 1, "clickCount": 1},
                                ),
                                driver.execute_cdp_cmd(
                                    "Input.dispatchMouseEvent",
                                    {"type": "mouseReleased", "x": arrow_x, "y": arrow_y, "button": "left", "buttons": 1, "clickCount": 1},
                                ),
                            ),
                        ))
                        strategies.append((
                            "cdp-center",
                            lambda: (
                                driver.execute_cdp_cmd(
                                    "Input.dispatchMouseEvent",
                                    {"type": "mouseMoved", "x": rect["cx"], "y": rect["cy"], "button": "left", "buttons": 1},
                                ),
                                driver.execute_cdp_cmd(
                                    "Input.dispatchMouseEvent",
                                    {"type": "mousePressed", "x": rect["cx"], "y": rect["cy"], "button": "left", "buttons": 1, "clickCount": 1},
                                ),
                                driver.execute_cdp_cmd(
                                    "Input.dispatchMouseEvent",
                                    {"type": "mouseReleased", "x": rect["cx"], "y": rect["cy"], "button": "left", "buttons": 1, "clickCount": 1},
                                ),
                            ),
                        ))

                    for strategy_name, strategy in strategies:
                        try:
                            strategy()
                        except Exception:
                            continue
                        time.sleep(0.45)
                        try:
                            driver.execute_script(
                                """
                                window.__pricingCountryCombobox = arguments[0];
                                window.__pricingCountryListId = arguments[0].getAttribute('aria-controls') || '';
                                """,
                                picker,
                            )
                        except Exception:
                            pass
                        try:
                            opened_state = driver.execute_script(
                                """
                                const picker = arguments[0];
                                const listId = picker.getAttribute('aria-controls') || '';
                                const controlled = listId ? document.getElementById(listId) : null;
                                const popup = controlled || [...document.querySelectorAll('[role="listbox"],[role="menu"],[data-radix-popper-content-wrapper]')]
                                    .find(el => {
                                        const r = el.getBoundingClientRect();
                                        return r.width > 40 && r.height > 40 && r.bottom > 0 && r.top < window.innerHeight;
                                    });
                                return {
                                    expanded: picker.getAttribute('aria-expanded') === 'true',
                                    popupVisible: !!popup,
                                };
                                """,
                                picker,
                            ) or {}
                        except Exception:
                            opened_state = {}
                        if opened_state.get("expanded") or opened_state.get("popupVisible"):
                            print(f"    -> Đã mở country combobox bằng {strategy_name}")
                            return text
            except Exception:
                pass
            try:
                return driver.execute_script(
                    """
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                    let picker = window.__pricingCountryCombobox;
                    if (!picker || !document.contains(picker)) {
                        const roots = [
                            document.querySelector('#modal-account-payment'),
                            ...document.querySelectorAll('dialog[open], dialog'),
                            document,
                        ].filter(Boolean);
                        const labels = [...roots.flatMap(root => [...(root.querySelectorAll ? root.querySelectorAll('[id]') : [])])]
                            .filter(el => {
                                const t = norm(el.innerText || el.textContent || '').toLowerCase();
                                return t === 'quốc gia và tiền tệ'
                                    || t === 'country and currency'
                                    || t.includes('quốc gia') && t.includes('tiền tệ')
                                    || t.includes('country') && t.includes('currency');
                            });
                        const found = [];
                        for (const label of labels) {
                            for (const root of roots) {
                                found.push(...(root.querySelectorAll ? root.querySelectorAll(`button[role="combobox"][aria-labelledby~="${CSS.escape(label.id)}"]`) : []));
                            }
                        }
                        picker = found.find(el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        });
                    }
                    if (!picker) return '';
                    const text = norm(picker.innerText || picker.textContent || '');
                    picker.scrollIntoView({block: 'center', inline: 'center'});
                    picker.click();
                    window.__pricingCountryCombobox = picker;
                    window.__pricingCountryListId = picker.getAttribute('aria-controls') || '';
                    return text;
                    """
                ) or ""
            except Exception:
                return ""

        def click_indonesia_option():
            try:
                return driver.execute_script(
                    """
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                    const listId = window.__pricingCountryListId || '';
                    const controlled = listId ? document.getElementById(listId) : null;
                    const roots = controlled ? [controlled] : [...document.querySelectorAll('[role="listbox"],[role="menu"],[data-radix-popper-content-wrapper]')];
                    const root = roots.find(el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight;
                    }) || document;
                    const candidates = [...root.querySelectorAll('[role="option"],[role="menuitem"],[data-radix-collection-item],button,[role="button"],li,div')]
                        .map(el => ({el, text: norm(el.innerText || el.textContent || ''), r: el.getBoundingClientRect()}))
                        .filter(x => x.r.width > 0 && x.r.height > 0 && x.text.length <= 80);
                    for (const x of candidates) {
                        const lower = x.text.toLowerCase();
                        if (!(lower === 'indonesia' || lower.includes('indonesia'))) continue;
                        if (x.r.bottom < 0 || x.r.top > window.innerHeight) {
                            x.el.scrollIntoView({block: 'center', inline: 'center'});
                        }
                        x.el.scrollIntoView({block: 'center', inline: 'center'});
                        x.el.click();
                        return x.text;
                    }
                    return '';
                    """
                ) or ""
            except Exception:
                return ""

        def focus_country_search_and_type_indonesia():
            """Một số dropdown country có ô search ẩn/active typeahead."""
            try:
                # Typeahead của Radix sẽ nhảy gần "Indonesia" nếu combobox đang focus.
                ActionChains(driver).send_keys("indo").perform()
                time.sleep(0.5)
                return True
            except Exception:
                return False

        def scroll_country_dropdown(step=120):
            """Cuộn container dropdown country, không cuộn cả trang pricing."""
            try:
                return bool(driver.execute_script(
                    """
                    const step = arguments[0] || 120;
                    const listId = window.__pricingCountryListId || '';
                    const controlled = listId ? document.getElementById(listId) : null;
                    const roots = controlled ? [controlled] : [...document.querySelectorAll('[role="listbox"],[role="menu"],[data-radix-popper-content-wrapper]')];
                    const findScrollable = root => {
                        if (!root) return null;
                        const all = [root, ...root.querySelectorAll('*')];
                        return all.find(el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 80 && r.height > 40 && el.scrollHeight > el.clientHeight + 8
                                && r.top < window.innerHeight && r.bottom > 0;
                        });
                    };
                    const target = roots.map(findScrollable).find(Boolean);
                    if (!target) {
                        window.dispatchEvent(new WheelEvent('wheel', {deltaY: step, bubbles: true}));
                        return false;
                    }
                    target.scrollTop += step;
                    target.dispatchEvent(new WheelEvent('wheel', {deltaY: step, bubbles: true}));
                    return true;
                    """,
                    step,
                ))
            except Exception:
                try:
                    ActionChains(driver).scroll_by_amount(0, step).perform()
                except Exception:
                    pass
                return False

        def jump_country_dropdown(progress_ratio):
            """Nhảy nhanh tới một vị trí sâu trong dropdown country."""
            try:
                return bool(driver.execute_script(
                    """
                    const ratio = Math.max(0, Math.min(1, arguments[0] || 0));
                    const listId = window.__pricingCountryListId || '';
                    const controlled = listId ? document.getElementById(listId) : null;
                    const roots = controlled ? [controlled] : [...document.querySelectorAll('[role="listbox"],[role="menu"],[data-radix-popper-content-wrapper]')];
                    const findScrollable = root => {
                        if (!root) return null;
                        const all = [root, ...root.querySelectorAll('*')];
                        return all.find(el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 80 && r.height > 40 && el.scrollHeight > el.clientHeight + 8
                                && r.top < window.innerHeight && r.bottom > 0;
                        });
                    };
                    const target = roots.map(findScrollable).find(Boolean);
                    if (!target) return false;
                    const maxScroll = Math.max(0, target.scrollHeight - target.clientHeight);
                    target.scrollTop = maxScroll * ratio;
                    target.dispatchEvent(new WheelEvent('wheel', {deltaY: 300, bubbles: true}));
                    return true;
                    """,
                    progress_ratio,
                ))
            except Exception:
                return False

        def reset_country_dropdown_scroll():
            try:
                driver.execute_script(
                    """
                    const listId = window.__pricingCountryListId || '';
                    const controlled = listId ? document.getElementById(listId) : null;
                    const roots = controlled ? [controlled] : [...document.querySelectorAll('[role="listbox"],[role="menu"],[data-radix-popper-content-wrapper]')];
                    for (const root of roots) {
                        if (!root) continue;
                        for (const el of [root, ...root.querySelectorAll('*')]) {
                            if (el.scrollHeight > el.clientHeight + 8) el.scrollTop = 0;
                        }
                    }
                    """
                )
            except Exception:
                pass

        def select_indonesia_pricing_country():
            """Đổi country picker ở cuối pricing sang Indonesia trước khi click trial."""
            print("  🌏 Đổi quốc gia pricing sang Indonesia...")

            opened = False
            for _ in range(3):
                picker_text = scroll_to_pricing_country_picker()
                if picker_text:
                    print(f"    -> Tìm thấy country picker: {picker_text}")
                opened_text = click_current_country_picker()
                if opened_text:
                    print(f"    -> Đã mở chọn quốc gia từ: {opened_text}")
                    opened = True
                    time.sleep(0.8)
                    break
                time.sleep(0.5)

            if not opened:
                opened = open_country_selector_after_scroll()

            if not opened:
                print("  ⚠️ Không mở được dropdown quốc gia")
                return False

            clicked = False
            reset_country_dropdown_scroll()
            focus_country_search_and_type_indonesia()
            try:
                indonesia_option = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'Indonesia')]"))
                )
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", indonesia_option)
                time.sleep(0.5)
                try:
                    driver.execute_script("arguments[0].click();", indonesia_option)
                except Exception:
                    indonesia_option.click()
                print("    -> Đã chọn quốc gia: Indonesia")
                clicked = True
            except Exception:
                pass

            if not clicked:
                # Indonesia thường nằm khá sâu trong list; nhảy cóc xuống sâu trước rồi mới rà mịn.
                for ratio in (0.45, 0.62, 0.76, 0.86):
                    jump_country_dropdown(ratio)
                    time.sleep(0.18)
                    clicked_text = click_indonesia_option()
                    if clicked_text:
                        print(f"    -> Đã chọn quốc gia: {clicked_text}")
                        clicked = True
                        break

            for i in range(140):
                if clicked:
                    break
                clicked_text = click_indonesia_option()
                if clicked_text:
                    print(f"    -> Đã chọn quốc gia: {clicked_text}")
                    clicked = True
                    break
                try:
                    # Giai đoạn đầu nhảy nhanh xuống sâu, sau đó mới rà mịn để bắt đúng option.
                    if i < 18:
                        scroll_country_dropdown(step=180)
                    elif i < 48:
                        scroll_country_dropdown(step=90)
                    else:
                        scroll_country_dropdown(step=45)
                    if i % 10 == 0:
                        focus_country_search_and_type_indonesia()
                    if i % 8 == 0:
                        ActionChains(driver).send_keys(Keys.ARROW_DOWN).perform()
                except Exception:
                    pass
                time.sleep(0.06)

            if not clicked:
                print("  ⚠️ Không tìm thấy lựa chọn Indonesia")
                return False

            deadline = time.time() + 25
            while time.time() < deadline:
                try:
                    combobox_text = driver.execute_script(
                        """
                        const picker = window.__pricingCountryCombobox;
                        if (!picker || !document.contains(picker)) return '';
                        return (picker.innerText || picker.textContent || '').trim();
                        """
                    ) or ""
                except Exception:
                    combobox_text = ""
                if "indonesia" in combobox_text.lower():
                    print(f"  ✅ Pricing đã chuyển sang quốc gia: {combobox_text}")
                    time.sleep(1)
                    return True
                try:
                    body_text = driver.execute_script("return document.body.innerText || ''") or ""
                except Exception:
                    body_text = ""
                lowered = body_text.lower()
                if "indonesia" in lowered or "idr" in lowered:
                    print("  ✅ Pricing đã chuyển sang Indonesia/IDR")
                    time.sleep(1)
                    return True
                time.sleep(0.5)

            print("  ⚠️ Chưa xác nhận được Indonesia/IDR sau khi chọn quốc gia")
            return False

        def wait_for_checkout_url(timeout=45):
            deadline = time.time() + timeout
            last_url = ""
            while time.time() < deadline:
                try:
                    current_url = driver.current_url
                except Exception:
                    current_url = ""
                if current_url and current_url != last_url:
                    print(f"  URL sau click trial: {current_url}")
                    last_url = current_url
                lowered = current_url.lower()
                if "/checkout/" in lowered or "checkout" in lowered or "pay.openai.com" in lowered:
                    return current_url
                time.sleep(0.5)
            return ""

        def has_pricing_modal_plan_grid():
            try:
                return bool(driver.execute_script(
                    """
                    const grid = document.querySelector('[data-testid="pricing-modal-plan-grid"]');
                    if (!grid) return false;
                    const r = grid.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                    """
                ))
            except Exception:
                return False

        def click_plus_button_in_modal_grid():
            try:
                plus_btn = driver.execute_script(
                    """
                    const grid = document.querySelector('[data-testid="pricing-modal-plan-grid"]');
                    if (!grid) return null;
                    const card = grid.querySelector('[data-testid="plus-pricing-modal-column"], #plus-pricing');
                    if (!card) return null;
                    const text = (card.innerText || card.textContent || '').toLowerCase();
                    if (!text.includes('plus')) return null;
                    if (
                        !text.includes('miễn phí')
                        && !text.includes('free')
                        && !text.includes('0')
                        && !text.includes('thời gian có hạn')
                        && !text.includes('limited time')
                        && !text.includes('tháng đầu tiên')
                    ) {
                        return null;
                    }
                    const btn = card.querySelector('button[data-testid="select-plan-button-plus-upgrade"]');
                    if (!btn) return null;
                    const r = btn.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return null;
                    btn.scrollIntoView({block: 'center', inline: 'center'});
                    return btn;
                    """
                )
                if not plus_btn:
                    return False
                print("  ✅ Bắt trực tiếp được nút Plus trong pricing-modal-plan-grid")
                time.sleep(0.5)
                try:
                    plus_btn.click()
                except Exception:
                    try:
                        driver.execute_script("arguments[0].click();", plus_btn)
                    except Exception:
                        ActionChains(driver).move_to_element(plus_btn).click().perform()
                checkout_url = wait_for_checkout_url(timeout=45)
                if checkout_url:
                    print("✅ Đã load ra trang checkout, dừng bot tại checkout để thanh toán tay")
                    print(f"🔗 {checkout_url}")
                    return "__MANUAL_CHECKOUT_READY__"
                print("⚠️ Đã click nút Plus trực tiếp trong grid nhưng chưa thấy checkout")
                return False
            except Exception:
                return False
        
        def find_and_click_subscribe(retry_count=0):
            if retry_count > 3: return False

            print("  🧹 Dọn vật cản trong pricing trước khi chọn card Plus...")
            dismiss_chatgpt_onboarding_if_present(driver, max_rounds=6)

            using_modal_grid = has_pricing_modal_plan_grid()
            if using_modal_grid:
                print("  🧩 Đang dùng UI pricing-modal-plan-grid: cuộn cuối trang -> đổi Indonesia -> bấm Plus")

            # Đảm bảo đang ở tab Personal/cá nhân, không phải Business/Team
            print("  🔘 Đảm bảo chọn tab cá nhân...")
            if not ensure_personal_pricing_tab():
                print("  ❌ Không ép được pricing về tab Cá nhân, dừng để tránh click nhầm Doanh nghiệp")
                return False
            print("  🖱️ Cuộn trang pricing xuống vùng chọn quốc gia...")
            scroll_pricing_page_to_country_section()
            time.sleep(0.8)
            scroll_pricing_page_to_country_section()
            if not select_indonesia_pricing_country():
                print("  ⚠️ Chưa đổi được quốc gia sang Indonesia, không click trial để tránh sai giá/quốc gia")
                return False
            print("  🔘 Chọn lại tab cá nhân sau khi đổi quốc gia...")
            if not ensure_personal_pricing_tab():
                print("  ❌ Sau khi đổi quốc gia vẫn đang ở tab Doanh nghiệp, dừng để tránh click sai")
                return False
            if has_plus_without_trial_button():
                print("  🚫 Sau khi đã chuyển Indonesia vẫn chỉ thấy Dùng bản Plus, account không có trial")
                return "NO_TRIAL"

            # Tìm nút nhận dùng thử miễn phí của gói Plus
            # Không phụ thuộc đơn vị tiền tệ. Card đúng có Plus + dấu hiệu promo/free + nút ưu đãi.
            print("  🔘 Tìm đúng card Plus có ưu đãi miễn phí...")
            if using_modal_grid:
                modal_grid_result = click_plus_button_in_modal_grid()
                if modal_grid_result:
                    return modal_grid_result

            buttons_xpaths = [
                '//*[@data-testid="pricing-modal-plan-grid"]//*[@data-testid="plus-pricing-modal-column"]//button[@data-testid="select-plan-button-plus-upgrade"]',
                '//*[@data-testid="pricing-modal-plan-grid"]//*[@id="plus-pricing"]//button[@data-testid="select-plan-button-plus-upgrade"]',
                '//*[@data-testid="plus-pricing-modal-column"]//button[@data-testid="select-plan-button-plus-upgrade"]',
                '//*[@id="plus-pricing"]//button[@data-testid="select-plan-button-plus-upgrade"]',
                (
                    '//*[(@data-testid="plus-pricing-modal-column" or @id="plus-pricing") '
                    'and contains(normalize-space(.), "Plus") '
                    'and (contains(normalize-space(.), "miễn phí") '
                    'or contains(normalize-space(.), "free") '
                    'or contains(normalize-space(.), "0") '
                    'or contains(normalize-space(.), "THỜI GIAN CÓ HẠN") '
                    'or contains(normalize-space(.), "LIMITED TIME"))]'
                    '//button[contains(normalize-space(.), "Nhận ưu đãi miễn phí") '
                    'or contains(normalize-space(.), "Nhận dùng thử miễn phí") '
                    'or contains(normalize-space(.), "Try for free") '
                    'or contains(normalize-space(.), "Start trial") '
                    'or contains(normalize-space(.), "Free trial")]'
                ),
            ]
            
            for xpath in buttons_xpaths:
                try:
                    btns = driver.find_elements(By.XPATH, xpath)
                    for btn in btns:
                        if btn.is_displayed():
                            try:
                                card_text = driver.execute_script(
                                    """
                                    const btn = arguments[0];
                                    const card = btn.closest('[data-testid="plus-pricing-modal-column"], #plus-pricing, [data-pricing-column-content]');
                                    return card ? card.innerText : btn.innerText;
                                    """,
                                    btn,
                                ) or ""
                            except Exception:
                                card_text = btn.text or ""
                            card_text_lower = card_text.lower()
                            if "plus" not in card_text_lower:
                                print("  ⚠️ Bỏ qua nút không nằm trong card Plus")
                                continue
                            first_line = (card_text_lower.splitlines() or [""])[0].strip()
                            if any(blocked in card_text_lower for blocked in ("enterprise", "business", "doanh nghiệp", "team")):
                                print("  ⚠️ Bỏ qua card/vùng có dấu hiệu Doanh nghiệp/Team")
                                continue
                            if "pro" in first_line and "plus" not in first_line:
                                print("  ⚠️ Bỏ qua card Pro")
                                continue
                            if not any(
                                marker in card_text_lower
                                for marker in ("miễn phí", "free", "0", "thời gian có hạn", "limited time", "ưu đãi")
                            ):
                                print("  ⚠️ Card Plus chưa có dấu hiệu ưu đãi/free trial, bỏ qua")
                                continue
                            print(f"  Tìm thấy nút trial Plus: {btn.text}")
                            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                            time.sleep(0.8)
                            try:
                                btn.click()
                                checkout_url = wait_for_checkout_url(timeout=45)
                                if checkout_url:
                                    print("✅ Đã load ra trang checkout, dừng bot tại checkout để thanh toán tay")
                                    print(f"🔗 {checkout_url}")
                                    return "__MANUAL_CHECKOUT_READY__"
                                print("⚠️ Đã click trial nhưng chưa thấy URL checkout")
                                return False
                            except Exception as e:
                                print(f"  ⚠️ Click bị chặn, thử dọn popup lần nữa... {e}")
                                dismiss_chatgpt_onboarding_if_present(driver, max_rounds=4)
                                time.sleep(2)
                                return find_and_click_subscribe(retry_count + 1)
                except:
                    continue
            
            # Nếu vẫn chưa thấy card Plus hợp lệ, thử refresh pricing một lần rồi tìm lại.
            if retry_count == 0:
                 print("  ⚠️ Chưa tìm thấy nút trực tiếp, thử làm mới trang...")
                 driver.refresh()
                 time.sleep(3)
                 return find_and_click_subscribe(retry_count + 1)
                 
            return False

        trial_checkout_url = find_and_click_subscribe()
        if trial_checkout_url == "NO_TRIAL":
             return "NO_TRIAL"
        if not trial_checkout_url:
             print("❌ Sau nhiều lần retry vẫn không tìm thấy nút đăng ký Plus")
             try: driver.save_screenshot("debug_no_plus_btn.png")
             except: pass
             return False
        if isinstance(trial_checkout_url, str):
            return trial_checkout_url
        
        print("✅ Đã click nút đăng ký Plus")     
            
        print("⏳ Chờ trang thanh toán tải, phát hiện thông minh...")
        # Thay sleep(10) cố định bằng giám sát động phần tử form
        page_loaded = False
        start_wait = time.time()
        while time.time() - start_wait < 30:
            # Kiểm tra có input hoặc iframe không
            inputs = driver.find_elements(By.CSS_SELECTOR, "input, iframe")
            if len(inputs) > 3:
                # Kiểm tra thêm dấu hiệu liên quan thanh toán
                page_source = driver.page_source.lower()
                if "stripe" in page_source or "card" in page_source or "payment" in page_source:
                    print("  ✅ Phát hiện phần tử form thanh toán, trang đã sẵn sàng")
                    page_loaded = True
                    break
            time.sleep(1)
        
        if not page_loaded:
            print("⚠️ Trang có vẻ tải quá lâu, thử tiếp tục điền...")
        
        time.sleep(2) # Đệm thêm
        
        # -------------------------------------------------------------------------
        # 3. Điền form thanh toán
        # -------------------------------------------------------------------------
        print("💳 Bắt đầu điền thông tin thanh toán...")
        wait_input = WebDriverWait(driver, 15)
        
        # Hàm phụ: tìm phần tử trong ngữ cảnh hiện tại
        def find_visible(selector):
            try:
                el = driver.find_element(By.CSS_SELECTOR, selector)
                if el.is_displayed(): return el
            except: 
                pass
            try:
                el = driver.find_element(By.XPATH, selector) # Tương thích XPATH
                if el.is_displayed(): return el
            except:
                pass
            return None

        # Hàm phụ: duyệt tìm và thực thi thao tác
        def run_in_all_frames(action_name, action_func):
            # 1. document chính
            if action_func():
                print(f"  ✅ {action_name} (document chính)")
                return True
            
            # 2. Duyệt iframe
            driver.switch_to.default_content()
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            for i, frame in enumerate(iframes):
                try:
                    driver.switch_to.frame(frame)
                    if action_func():
                        print(f"  ✅ {action_name} (iframe[{i}])")
                        driver.switch_to.default_content()
                        return True
                    driver.switch_to.default_content()
                except:
                    try: driver.switch_to.default_content()
                    except: pass
            
            print(f"  ⚠️ Chưa hoàn tất: {action_name}")
            return False

        # ============== 1. Tự phát hiện quốc gia hiện tại ==============
        current_country_code = "JP" # Mặc định dự phòng
        detected_country_name = "Unknown"

        def detect_country():
            nonlocal current_country_code, detected_country_name
            
            # Thử tìm dropdown quốc gia
            # 1. Tìm Select
            try:
                sel = find_visible('select[name="billingAddressCountry"], select[id^="Field-countryInput"]')
                if sel:
                    val = sel.get_attribute('value')
                    if val in ["US", "United States", "Mỹ"]:
                        current_country_code = "US"
                        detected_country_name = "United States"
                    elif val in ["JP", "Japan", "Nhật Bản"]:
                        current_country_code = "JP"
                        detected_country_name = "Japan"
                    else:
                        current_country_code = "JP" # Quốc gia khác tạm xử lý như JP, có thể mở rộng theo nhu cầu
                        detected_country_name = val
                    return True
            except: pass

            # 2. Tìm dropdown giả lập bằng div
            try:
                 # Tìm div gần label quốc gia hoặc Country
                 dropdown_div = find_visible('//label[contains(text(), "Country")]/following::div[contains(@class, "Select")][1]')
                 if not dropdown_div:
                     # Thử tìm div chứa tên quốc gia đã biết
                     dropdown_div = find_visible('//*[contains(text(), "United States") or contains(text(), "Mỹ") or contains(text(), "Japan") or contains(text(), "Nhật Bản")]/ancestor::div[contains(@class, "Select") or contains(@class, "Input")][1]')
                 
                 if dropdown_div:
                     text = dropdown_div.text
                     if any(k in text for k in ["United States", "Mỹ", "US"]):
                         current_country_code = "US"
                         detected_country_name = "United States"
                     elif any(k in text for k in ["Japan", "Nhật Bản"]):
                         current_country_code = "JP"
                         detected_country_name = "Japan"
                     else:
                        current_country_code = "JP"
                        detected_country_name = text
                     return True
            except: pass
            
            # 3. Dự phòng: tìm text Mỹ hoặc United States hiển thị trên trang và ở vị trí phía trên
            try:
                # Tìm text Mỹ trong vùng form
                us_text = find_visible('//form//div[contains(text(), "Mỹ") or contains(text(), "United States")]')
                if us_text:
                     current_country_code = "US"
                     detected_country_name = "United States (Text Match)"
                     return True
            except: pass
            
            return False

        print("🌏 Tự phát hiện quốc gia hiện tại...")
        run_in_all_frames("Phát hiện quốc gia", detect_country)
        print(f"   -> Kết quả phát hiện: {detected_country_name} (Code: {current_country_code})")
        print("   -> Sẽ tạo địa chỉ thực tế của quốc gia này để điền")

        # Tạo thông tin hóa đơn ngẫu nhiên theo quốc gia tương ứng
        billing_info = generate_billing_info(current_country_code)
        
        # ============== 2. Điền họ tên ==============
        def fill_name():
            selectors = [
                 # ID phổ biến của Stripe
                 '#Field-nameInput', '#Field-billingNameInput', '#billingName',
                 'input[id^="Field-nameInput"]',
                 # Thuộc tính chung
                 'input[name="name"]', 'input[name="billingName"]', 
                 'input[id="billingName"]', 
                 # Placeholder tiếng Trung và tiếng Anh
                 'input[placeholder="Họ tên đầy đủ"]', 'input[placeholder="Full name"]',
                 'input[autocomplete="name"]', 'input[autocomplete="cc-name"]'
            ]
            for s in selectors:
                el = find_visible(s)
                if el:
                    el.clear()
                    type_slowly(el, billing_info["name"])
                    return True
            return False
            
        print(f"👤 Tìm và điền họ tên: {billing_info['name']}...")
        run_in_all_frames("Điền họ tên", fill_name)
        time.sleep(1)

        # ============== 3. Điền địa chỉ ==============
        def fill_address():
            # 1. Mã bưu chính (Zip)
            zip_el = find_visible('#Field-postalCodeInput, input[name="postalCode"], input[placeholder="Mã bưu chính"], input[placeholder="Zip code"]')
            if zip_el:
                zip_el.clear()
                type_slowly(zip_el, billing_info["zip"])
                print(f"  ✅ Điền mã bưu chính: {billing_info['zip']}")
                
                # === Sửa quan trọng ===
                # Sau khi điền mã bưu chính, Stripe thường cần request ngắn để hiện trường City/State
                # Nếu không chờ, lần tìm City/State sau đó có thể thất bại và lúc gửi chỉ có Zip
                print("  ⏳ Chờ trường địa chỉ cấp hai tải (3s)...")
                time.sleep(3)
            
            # 2. Bang/tỉnh (State)
            state_el = find_visible('#Field-administrativeAreaInput, #Field-koreanAdministrativeDistrictInput, select[name="state"], input[name="state"]')
            if state_el:
                try:
                    if state_el.tag_name == 'select':
                        state_el.send_keys(billing_info["state"])
                        state_el.send_keys(Keys.ENTER)
                    else:
                        state_el.clear()
                        type_slowly(state_el, billing_info["state"])
                        state_el.send_keys(Keys.ARROW_DOWN)
                        state_el.send_keys(Keys.ENTER)
                    print(f"  ✅ Điền bang/tỉnh: {billing_info['state']}")
                except: 
                    try:
                        state_el.click()
                        time.sleep(0.5)
                        ActionChains(driver).send_keys(billing_info["state"]).send_keys(Keys.ENTER).perform()
                    except: pass

            # 3. Thành phố (City)
            city_el = find_visible('#Field-localityInput, input[name="city"], input[placeholder="Thành phố"], input[placeholder="City"]')
            if city_el:
                city_el.clear()
                type_slowly(city_el, billing_info["city"])
                print(f"  ✅ Điền thành phố: {billing_info['city']}")

            # 4. Địa chỉ dòng 1
            line1_el = find_visible('#Field-addressLine1Input, input[name="addressLine1"], input[placeholder="Address line 1"]')
            if line1_el:
                line1_el.clear()
                type_slowly(line1_el, billing_info["address1"])
                time.sleep(0.5)
                # Một số popup autocomplete cần nhấn ESC để đóng
                try: ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                except: pass
                print(f"  ✅ Điền địa chỉ dòng 1: {billing_info['address1']}")
                
            return True

        print("🏠 Tìm và điền địa chỉ...")
        run_in_all_frames("Điền địa chỉ", fill_address)
        time.sleep(1)

        # ============== 4. Điền thẻ tín dụng ==============
        print("💳 Đang điền thông tin thẻ tín dụng...")
        card = CREDIT_CARD_INFO
        
        # Số thẻ
        if not handle_stripe_input(driver, 'Số thẻ', 'input[name="cardnumber"], input[placeholder*="Card number"], input[placeholder*="0000"], input[autocomplete="cc-number"]', card["number"]):
             print("❌ Nhập số thẻ thất bại")
        
        time.sleep(1)
        
        # Ngày hết hạn
        if not handle_stripe_input(driver, 'Ngày hết hạn', 
            'input[name="exp-date"], input[name="expirationDate"], input[id="cardExpiry"], input[placeholder="MM / YY"], input[autocomplete="cc-exp"]', 
            card["expiry"]):
            print("❌ Nhập ngày hết hạn thất bại")
            
        time.sleep(1)
        
        # CVC
        if not handle_stripe_input(driver, 'CVC', 'input[name="cvc"], input[name="securityCode"], input[id="cardCvc"], input[placeholder="CVC"]', card["cvc"]):
             print("❌ Nhập CVC thất bại")

        time.sleep(2)
        
        # ============== 5. Vòng lặp gửi và bổ sung ==============
        def loop_submit_and_fix():
            max_attempts = 5
            for attempt in range(max_attempts):
                print(f"🔄 Thử gửi ({attempt + 1}/{max_attempts})...")
                
                # 1. Click gửi
                driver.switch_to.default_content() # Nút thường nằm trong document chính
                try:
                    submit_btn = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit'], button[class*='Subscribe']")))
                    driver.execute_script("arguments[0].click();", submit_btn)
                    print("  🔘 Đã click nút gửi")
                except:
                    print("  ⚠️ Không tìm thấy nút gửi")
                
                time.sleep(3) # Chờ kết quả kiểm tra
                
                # -------------------------------
                # Mới: kiểm tra có captcha/mã xác minh hCaptcha hoặc Cloudflare không
                # -------------------------------
                try:
                    # Tìm iframe captcha/mã xác minh có thể có
                    captcha_frames = driver.find_elements(By.CSS_SELECTOR, "iframe[src*='hcaptcha'], iframe[src*='challenges'], iframe[title*='widget']")
                    for frame in captcha_frames:
                        if frame.is_displayed():
                            print("  ⚠️ Phát hiện mã xác minh, thử click...")
                            driver.switch_to.frame(frame)
                            try:
                                # hCaptcha / Cloudflare Checkbox phổ biến
                                checkbox = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#checkbox, .checkbox, #challenge-stage")))
                                checkbox.click()
                                print("    ✅ Đã click checkbox mã xác minh")
                                time.sleep(5) # Chờ xác minh thông qua
                            except Exception as e:
                                print(f"    ⚠️ Click mã xác minh thất bại: {e}")
                            
                            driver.switch_to.default_content()
                except:
                    driver.switch_to.default_content()

                # 2. Kiểm tra có lỗi Incomplete field không
                # Cần duyệt iframe để kiểm tra
                has_error = False
                driver.switch_to.default_content()
                frames = driver.find_elements(By.TAG_NAME, "iframe")
                all_frames = [None] + frames # None biểu thị document chính
                
                for frame in all_frames:
                    if frame:
                        try: driver.switch_to.frame(frame)
                        except: continue
                    else:
                        driver.switch_to.default_content()
                        
                    # Tìm lỗi chữ đỏ
                    errors = driver.find_elements(By.XPATH, '//*[contains(text(), "Trường này chưa đầy đủ") or contains(text(), "Incomplete field") or contains(text(), "Required")]')
                    
                    if errors:
                        print(f"  ⚠️ Phát hiện {len(errors)} trường chưa hoàn tất, đang bổ sung...")
                        has_error = True
                        
                        # --- US Chiến lược bổ sung ---

                        # 1. Kiểm tra địa chỉ dòng 1, thiếu sót phổ biến nhất
                        try:
                             line1_inputs = driver.find_elements(By.CSS_SELECTOR, '#Field-addressLine1Input, input[name="addressLine1"], input[placeholder="Address line 1"]')
                             for el in line1_inputs:
                                 if el.is_displayed() and not el.get_attribute('value'):
                                      print(f"    -> Điền bổ sung Address Line 1 ({billing_info['address1']})")
                                      el.clear()
                                      type_slowly(el, billing_info['address1'])
                                      # Đôi khi điền xong cần nhấn Enter
                                      try: el.send_keys(Keys.ENTER)
                                      except: pass
                        except Exception as e:
                            print(f"    debug: Điền bổ sung address1 ngoại lệ {e}")

                        # 2. Kiểm tra bang/State
                        state_inputs = driver.find_elements(By.CSS_SELECTOR, '#Field-administrativeAreaInput, select[name="state"], input[name="state"]')
                        for el in state_inputs:
                            try:
                                if el.is_displayed():
                                    print("    -> Điền bổ sung State (US mặc định New York)")
                                    if el.tag_name == 'select':
                                        el.send_keys("New York")
                                        el.send_keys(Keys.ENTER)
                                    else:
                                        el.send_keys("New York")
                                        el.send_keys(Keys.ARROW_DOWN)
                                        el.send_keys(Keys.ENTER)
                            except: pass

                        # Kiểm tra mã bưu chính
                        zip_inputs = driver.find_elements(By.CSS_SELECTOR, '#Field-postalCodeInput, input[name="postalCode"]')
                        for el in zip_inputs:
                            try:
                                if el.is_displayed() and not el.get_attribute('value'):
                                    print("    -> Điền bổ sung Zip (10001)")
                                    el.clear()
                                    type_slowly(el, "10001")
                            except: pass
                            
                        # Kiểm tra thành phố
                        city_inputs = driver.find_elements(By.CSS_SELECTOR, '#Field-localityInput, input[name="city"]')
                        for el in city_inputs:
                            try:
                                if el.is_displayed() and not el.get_attribute('value'):
                                    print("    -> Điền bổ sung City (New York)")
                                    el.clear()
                                    type_slowly(el, "New York")
                            except: pass
                            
                    driver.switch_to.default_content()
                    if has_error: break # Chỉ cần phát hiện lỗi thì thoát vòng iframe để click gửi
                
                if not has_error:
                    print("✅ Có vẻ không còn lỗi form, đang chờ kết quả...")
                    return True
                
                time.sleep(1)
            
            return False

        print("🚀 Vào vòng lặp gửi...")
        check_result = loop_submit_and_fix()

        print("✅ Quy trình gửi form kết thúc, đang chờ kết quả thanh toán hoặc chuyển trang...")
        
        # Thanh toán có thể cần thời gian xác minh lâu hơn
        # Thăm dò thay đổi URL
        start_time = time.time()
        while time.time() - start_time < 30:
            current_url = driver.current_url
            print(f"  URL hiện tại: {current_url}")
            
            # Tín hiệu thành công 1: Quay về trang chủ
            if ("chatgpt.com" in current_url or "chat.openai.com" in current_url) and "pricing" not in current_url and "payment" not in current_url:
                 print("✅ Phát hiện chuyển về trang chủ, đăng ký thành công！")
                 
                 # Nhân tiện xử lý popup chào mừng để tiện hủy đăng ký sau đó
                 try:
                    okay_btn = driver.find_element(By.XPATH, '//button[contains(., "Okay") or contains(., "Bắt đầu") or contains(., "Let")]')
                    okay_btn.click()
                    print("  -> Đã đóng popup chào mừng")
                 except: pass
                 
                 return True

            # Tín hiệu thành công 2: Xuất hiện popup Welcome
            try:
                if driver.find_element(By.XPATH, '//div[contains(text(), "ChatGPT")]//div[contains(text(), "Tips")]').is_displayed():
                    print("✅ Phát hiện popup chào mừng, đăng ký thành công！")
                    return True
            except: pass
            
            # Tín hiệu thất bại
            try:
                 error_msg = driver.find_element(By.CSS_SELECTOR, '.StripeElement--invalid, .error-message, [role="alert"]')
                 if error_msg and error_msg.is_displayed():
                     print(f"❌ Thanh toán gặp lỗi: {error_msg.text}")
                     # Không bỏ cuộc ngay, đôi khi chỉ là lỗi tạm thời
            except:
                 pass
                 
            time.sleep(2)

        print("❌ Hết thời gian chờ chuyển trang và vẫn ở trang thanh toán, đăng ký có thể thất bại.")
        return False
            
    except Exception as e:
        print(f"❌ Quy trình đăng ký gặp lỗi: {e}")
        return False


def cancel_subscription(driver):
    """
    Hủy đăng ký
    """
    print("\n" + "=" * 50)
    print("🛑 Bắt đầu quy trình hủy đăng ký")
    print("=" * 50)
    
    wait = WebDriverWait(driver, 20)
    
    try:
        # Đảm bảo quay về trang chủ
        if "chatgpt.com" not in driver.current_url:
            driver.get("https://chatgpt.com")
        
        # ===== Chờ trang tải hoàn tất =====
        print("⏳ Chờ trang tải hoàn tất...")
        for _ in range(10):  # Chờ tối đa 20 giây
            try:
                # Phần tử nhận diện: ô nhập hoặc nút avatar
                driver.find_element(By.ID, "prompt-textarea")
                print("  ✅ Trang đã tải xong")
                break
            except:
                time.sleep(2)
        
        time.sleep(2)  # Đệm thêm
            
        # 🧹 Dọn popup chào mừng có thể có (Critical!)
        print("🧹 Kiểm tra và dọn popup chào mừng...")
        for _ in range(3):
            try:
                welcomes = driver.find_elements(By.XPATH, '//button[contains(., "Okay") or contains(., "Bắt đầu") or contains(., "Let")]')
                clicked = False
                for btn in welcomes:
                    if btn.is_displayed():
                        print(f"  -> Click đóng popup chào mừng: {btn.text}")
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(1)
                        clicked = True
                if not clicked:
                     break
            except:
                pass
            time.sleep(1)
        
        # ===== Mở menu cá nhân (có retry) =====
        print("🔘 Mở menu cá nhân...")
        menu_opened = False
        for attempt in range(3):
            try:
                # Thử nhiều selector để tìm avatar/menu
                selectors = [
                    'div[data-testid="user-menu"]',
                    '.text-token-text-secondary',
                    '//div[contains(@class, "group relative")]'
                ]
                
                for sel in selectors:
                    try:
                        if sel.startswith('//'):
                            btn = driver.find_element(By.XPATH, sel)
                        else:
                            btn = driver.find_element(By.CSS_SELECTOR, sel)
                        btn.click()
                        menu_opened = True
                        break
                    except:
                        continue
                
                if menu_opened:
                    print(f"  ✅ Mở menu thành công (Lần {attempt+1} lần thử)")
                    break
                    
            except Exception as e:
                print(f"  ⚠️ Lần thử {attempt+1} thất bại: {e}")
            
            if not menu_opened:
                print(f"  🔄 Chờ 2 giây rồi retry...")
                time.sleep(2)
        
        if not menu_opened:
            print("❌ Sau nhiều lần retry vẫn không mở được menu cá nhân")
            return False
            
        
        time.sleep(2)
        
        # Debug: in nội dung menu
        try:
            menu = driver.find_element(By.CSS_SELECTOR, '[role="menu"], div[data-testid*="menu"]')
            print(f" Nội dung menu:\n{menu.text}")
        except:
            pass
        
        print("🔘 Click My Plan / gói của tôi...")
        found_my_plan = False
        try:
            # Ưu tiên tìm My plan / gói của tôi
            my_plan_btn = wait.until(EC.element_to_be_clickable((By.XPATH, '//div[contains(text(), "My plan") or contains(text(), "Gói của tôi")]')))
            my_plan_btn.click()
            found_my_plan = True
        except:
            print("⚠️ Không tìm thấy 'Gói của tôi', thử vào qua 'Cài đặt'...")
            
            try:
                # 1. Click Settings / cài đặt
                settings_btn = driver.find_element(By.XPATH, '//div[contains(text(), "Settings") or contains(text(), "Cài đặt")]')
                settings_btn.click()
                print("  -> Đã click 'Cài đặt'")
                time.sleep(2)
                
                # 2. Click Account / tài khoản bên trái nếu là tab
                # 3. Trong popup Cài đặt, click tab Account / Tài khoản
                print("  -> Chuyển sang tab 'Tài khoản'...")
                
                from selenium.webdriver.common.action_chains import ActionChains
                
                try:
                    # Dùng Selenium tìm chính xác nút Account
                    account_btns = driver.find_elements(By.XPATH, '//div[@role="dialog"]//button')
                    
                    for btn in account_btns:
                        try:
                            txt = btn.text.strip()
                            if txt == 'Tài khoản' or txt == 'Tài khoản' or txt.lower() == 'account':
                                print(f"  -> Tìm thấy và click nút Account: '{txt}'")
                                actions = ActionChains(driver)
                                actions.move_to_element(btn).click().perform()
                                time.sleep(1)
                                break
                        except:
                            continue
                except Exception as e:
                    print(f"  ⚠️ Lỗi khi click tab Tài khoản: {e}")
                
                time.sleep(1)  # Chờ trang chuyển

                # 3. Kiểm tra trạng thái hoặc click Manage
                # Ảnh chụp cho thấy nếu đã hủy sẽ hiển thị thông báo sẽ bị hủy vào ngày...
                try:
                    status_text = driver.find_element(By.XPATH, '//*[contains(text(), "Gói của bạn sẽ bị hủy vào") or contains(text(), "Your plan will be canceled")]')
                    print(f"  ℹ️ Phát hiện trạng thái đăng ký: {status_text.text}")
                    print("  ✅ Có vẻ đăng ký đã hủy, không tiếp tục nữa.")
                    return True
                except:
                    pass

                # 4. Click nút Manage trong vùng ChatGPT Plus
                print("  -> Tìm nút 'Manage' trong vùng ChatGPT Plus...")
                try:
                    # Cách 1: tìm vùng chứa ChatGPT Plus rồi tìm nút Manage trong đó
                    manage_btn = driver.find_element(By.XPATH, 
                        '//*[contains(text(), "ChatGPT Plus")]/ancestor::div[1]//button[contains(., "Quản lý") or contains(., "Manage")]')
                    manage_btn.click()
                    print("  -> Đã click nút Quản lý trong vùng ChatGPT Plus")
                except:
                    try:
                        # Cách 2: tìm nút Quản lý đầu tiên bên dưới tiêu đề Tài khoản
                        manage_btn = driver.find_element(By.XPATH, 
                            '//h2[contains(., "Tài khoản") or contains(., "Account")]/following::button[contains(., "Quản lý") or contains(., "Manage")][1]')
                        manage_btn.click()
                        print("  -> Đã click nút Quản lý bên dưới tiêu đề")
                    except:
                        try:
                            # Cách 3: tìm nút Manage ở phần trên trang, loại trừ vùng thanh toán
                            manage_btns = driver.find_elements(By.XPATH, '//button[contains(., "Quản lý") or contains(., "Manage")]')
                            for btn in manage_btns:
                                # Kiểm tra nút có nằm ở nửa trên trang không, vùng ChatGPT Plus thường ở trên
                                location = btn.location
                                if location['y'] < 400 and btn.is_displayed():  # Giả định nửa trên là y < 400
                                    btn.click()
                                    print(f"  -> Đã click nút Quản lý ở vị trí phía trên (y={location['y']})")
                                    break
                        except Exception as e:
                            print(f"  ❌ Không tìm thấy nút Quản lý: {e}")
                            return False
                
                time.sleep(2)
                
                # ---------------------------------------------------------
                # Nhánh mới: kiểm tra có phải menu xổ xuống trong ứng dụng không
                # ---------------------------------------------------------
                print("  -> Chờ menu xổ xuống xuất hiện...")
                time.sleep(2)  # Chờ animation menu
                
                try:
                    # Thử nhiều selector để tìm Hủy đăng ký / Cancel subscription
                    cancel_xpaths = [
                        '//*[contains(text(), "Hủy đăng ký")]',
                        '//*[contains(text(), "Cancel subscription")]',
                        '//div[contains(text(), "Hủy đăng ký")]',
                        '//span[contains(text(), "Hủy đăng ký")]',
                        '//button[contains(., "Hủy đăng ký")]'
                    ]
                    
                    cancel_item = None
                    for xp in cancel_xpaths:
                        try:
                            items = driver.find_elements(By.XPATH, xp)
                            for item in items:
                                if item.is_displayed():
                                    cancel_item = item
                                    print(f"  -> Tìm thấy nút hủy: {item.text}")
                                    break
                        except: pass
                        if cancel_item: break
                    
                    if cancel_item:
                        print("  -> Click 'Hủy đăng ký'...")
                        driver.execute_script("arguments[0].click();", cancel_item)
                        time.sleep(2)
                        
                        # Xử lý popup xác nhận
                        print("  -> Chờ popup xác nhận...")
                        confirm_xpaths = [
                            '//button[contains(., "Hủy đăng ký")]',
                            '//button[contains(., "Cancel subscription")]',
                            '//div[@role="dialog"]//button[contains(@class, "danger")]'
                        ]
                        
                        for xp in confirm_xpaths:
                            try:
                                confirm_btns = driver.find_elements(By.XPATH, xp)
                                for btn in confirm_btns:
                                    if btn.is_displayed() and ("Hủy" in btn.text or "Cancel" in btn.text):
                                        driver.execute_script("arguments[0].click();", btn)
                                        print("✅ Đã click xác nhận hủy cuối cùng!")
                                        return True
                            except: pass
                        
                        print("  ⚠️ Không click được nút xác nhận")
                    else:
                        print("  ℹ️ Không phát hiện menu hủy trong ứng dụng")
                        
                except Exception as e:
                    print(f"  ℹ️ Ngoại lệ trong quy trình hủy trong ứng dụng: {e}")
                
                # ---------------------------------------------------------
                # Nhánh cũ: chuyển tới Stripe Billing Portal
                # ---------------------------------------------------------
                # Nếu phía trên không tìm thấy menu, có thể là phiên bản cũ đã chuyển sang tab mới
                pass
                
            except Exception as e:
                print(f"❌ Hủy qua trang Settings thất bại: {e}")
                return False
        else:
             print("🔘 Click quản lý đăng ký theo đường My Plan...")
             try:
                manage_btn = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[contains(text(), "Manage my subscription") or contains(text(), "Quản lý đăng ký của tôi")]')))
                manage_btn.click()
             except:
                print("❌ Không tìm thấy nút quản lý đăng ký")
                return False

        time.sleep(5)
        print("🌐 Chuyển tới Billing Portal...")
        
        print("🔘 Tìm nút hủy...")
        try:
             # Trang Stripe Portal
             # Đôi khi cần chuyển iframe trước, thường là cửa sổ mới hoặc chuyển trong trang hiện tại
            cancel_btn = wait.until(EC.presence_of_element_located((By.XPATH, '//button[contains(., "Cancel plan") or contains(., "Hủy gói")]')))
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", cancel_btn)
            time.sleep(1)
            cancel_btn.click()
        except:
             # Đôi khi là Cancel trial
            try:
                cancel_btn = driver.find_element(By.XPATH, '//button[contains(., "Cancel trial") or contains(., "Hủy dùng thử")]')
                cancel_btn.click()
            except:
                print("⚠️ Không tìm thấy nút hủy, có thể đã hủy hoặc cần can thiệp thủ công")
                return False
            
        time.sleep(2)
        print("🔘 Xác nhận hủy...")
        try:
            confirm_btn = wait.until(EC.element_to_be_clickable((By.XPATH, '//button[contains(., "Cancel plan") or contains(., "Confirm cancellation")]')))
            confirm_btn.click()
            print("✅ Đăng ký đã hủy！")
        except:
            print("⚠️ Không tìm thấy nút xác nhận hủy")
            
        time.sleep(3)
        return True
        
    except Exception as e:
        print(f"❌ Hủy đăng ký thất bại: {e}")
        return False
