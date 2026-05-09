"""
Script tự động đăng ký tài khoản ChatGPT
Điểm vào chương trình chính

Cách sử dụng:
    1. Sửa cấu hình trong config.py
    2. Chạy: python main.py

Cài đặt dependency:
    pip install undetected-chromedriver selenium requests

Chức năng:
    - Dùng Hotmail/Outlook có OAuth2 refresh token để nhận mã xác minh
    - Tự động hoàn tất quy trình đăng ký ChatGPT
    - Tự động trích xuất mã xác minh
    - Hỗ trợ đăng ký hàng loạt
"""

import time
import random
from contextlib import contextmanager
from typing import Any

from config import (
    TOTAL_ACCOUNTS,
    BATCH_INTERVAL_MIN,
    BATCH_INTERVAL_MAX,
    PAYMENT_CHECKOUT_ENABLED,
    PAYMENT_FLOW,
)
from utils import generate_random_password, save_to_txt, update_account_status, save_success_account
from email_service import HotmailAccount, create_temp_email, mark_account_result, wait_for_verification_email
from tempmail_service import TempMailAccount, wait_for_tempmail_verification_email
from browser import (
    BrowserStartupError,
    click_resend_email_button,
    create_driver,
    fill_signup_form,
    enter_verification_code,
    fill_profile_info,
    get_registered_profile_name,
)
from checkout_new import create_trial_checkout


MAX_PROFILE_RESTARTS = 3
CHECKOUT_HOME_STABILIZE_SECONDS = 5
OTP_WAIT_BEFORE_RESEND_SECONDS = 40
PROFILE_RESTART_REASON_MARKERS = (
    "Điền form đăng ký thất bại",
    "Không lấy được OTP hợp lệ",
    "Form inline lỗi",
    "Trang lỗi sau OTP",
    "Profile lỗi lặp quá",
    "Điền thông tin cá nhân thất bại",
    "Nhập mã xác minh thất bại",
    "Lỗi:",
)


def _email_context_bundle(email_context):
    """Return original hotmail line: mail|pass|refresh_token|client_id."""
    if not email_context:
        return ""
    if getattr(email_context, "provider", "") == "tmail":
        return getattr(email_context, "email", "") or ""
    fields = (
        getattr(email_context, "email", ""),
        getattr(email_context, "password", ""),
        getattr(email_context, "refresh_token", ""),
        getattr(email_context, "client_id", ""),
    )
    if not all(fields):
        return ""
    return "|".join(fields)


def should_restart_with_new_profile(result: dict[str, Any] | None) -> bool:
    """Xác định lỗi có nên bỏ profile hiện tại và chạy lại từ đầu với profile mới hay không."""
    if not isinstance(result, dict):
        return False
    if result.get("success"):
        return False
    reason = str(result.get("failure_reason") or "").strip()
    if not reason:
        return False
    return any(marker in reason for marker in PROFILE_RESTART_REASON_MARKERS)


def register_one_account_with_profile_retries(
    monitor_callback=None,
    email_context_override: HotmailAccount = None,
    account_password_override: str = None,
    return_details: bool = False,
    mark_result: bool = True,
    max_profile_restarts: int = MAX_PROFILE_RESTARTS,
):
    """Chạy lại toàn bộ account với profile mới nếu profile hiện tại bị kẹt/quá ngưỡng retry."""
    attempts = max(1, int(max_profile_restarts) + 1)
    last_result = None

    for attempt in range(1, attempts + 1):
        if attempt > 1:
            print(
                f"🔄 Profile trước không ổn, đang tạo profile mới và chạy lại từ đầu "
                f"({attempt}/{attempts})..."
            )

        result = register_one_account(
            monitor_callback=monitor_callback,
            email_context_override=email_context_override,
            account_password_override=account_password_override,
            return_details=return_details,
            mark_result=mark_result if attempt == attempts else False,
        )
        last_result = result

        if not return_details:
            email, password, success = result
            if success or attempt == attempts:
                return result
            print(
                "⚠️ Luồng CLI không có failure_reason chi tiết; "
                "không tự xác định được lỗi profile để retry thêm."
            )
            return result

        if result.get("success"):
            if mark_result and attempt < attempts and email_context_override:
                mark_account_result(email_context_override, True, "")
            return result

        if attempt == attempts or not should_restart_with_new_profile(result):
            if mark_result and attempt < attempts and email_context_override:
                mark_account_result(email_context_override, bool(result.get("success")), str(result.get("failure_reason") or "Đăng ký thất bại"))
            return result

        print(f"⚠️ Phát hiện lỗi theo profile: {result.get('failure_reason')}")

    return last_result


def register_one_account(
    monitor_callback=None,
    email_context_override: HotmailAccount = None,
    account_password_override: str = None,
    return_details: bool = False,
    mark_result: bool = True,
):
    """
    Đăng ký một tài khoản.
    :param monitor_callback: Hàm callback func(driver, step_name), dùng để chụp màn hình và kiểm tra ngắt.
    
    Trả về:
        tuple: (email, mật khẩu, có thành công hay không)
    """
    driver = None
    email = None
    email_context = None
    password = None
    success = False
    stopped = False
    failure_reason = "Đăng ký thất bại"
    browser_ready = False
    checkout_url = ""
    trial_success = False
    no_trial = False
    manual_checkout_ready = False
    timings = {}
    stage_started_at = time.perf_counter()

    def _return_result():
        if return_details:
            return {
                "email": email,
                "password": password,
                "success": success,
                "checkout_url": checkout_url,
                "trial_success": trial_success,
                "no_trial": no_trial,
                "manual_checkout_ready": manual_checkout_ready,
                "email_bundle": _email_context_bundle(email_context),
                "profile_name": get_registered_profile_name(driver) if driver else "",
                "failure_reason": failure_reason,
                "timings": timings,
            }
        return email, password, success

    def _wait_verification_code(account, timeout=None, since_ts=None, exclude_codes=None, baseline_message_ids=None):
        if getattr(account, "provider", "") == "tmail":
            return wait_for_tempmail_verification_email(
                account,
                timeout=timeout,
                since_ts=since_ts,
                exclude_codes=exclude_codes,
                baseline_message_ids=baseline_message_ids,
            )
        return wait_for_verification_email(
            account,
            timeout=timeout,
            since_ts=since_ts,
            exclude_codes=exclude_codes,
            baseline_message_ids=baseline_message_ids,
        )

    @contextmanager
    def _timed_stage(stage_name: str):
        started = time.perf_counter()
        print(f"⏱️ Bắt đầu khâu: {stage_name}")
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            timings[stage_name] = round(elapsed, 2)
            print(f"⏱️ Khâu {stage_name} mất {elapsed:.2f}s")
    
    # Hàm phụ trợ: gọi callback
    def _report(step_name):
        if monitor_callback and driver:
            monitor_callback(driver, step_name)

    try:
        # 1. Khởi tạo trình duyệt trước để lỗi GPM/Chrome không làm tiêu hao mail.
        with _timed_stage("init_browser"):
            driver = create_driver()
        browser_ready = True
        print("✅ Trình duyệt đã khởi tạo")
        _report("init_browser")

        # 2. Lấy tài khoản Hotmail sau khi browser đã sẵn sàng.
        print("📧 Đang lấy tài khoản Hotmail...")
        if email_context_override:
            email_context = email_context_override
            email = email_context.email
            print(f"📧 Đang sử dụng Hotmail từ input: {email}")
        else:
            email, email_context = create_temp_email()
        if not email:
            print("❌ Không lấy được tài khoản Hotmail, dừng đăng ký")
            return _return_result()
        
        # 3. Tạo mật khẩu ngẫu nhiên
        password = account_password_override or generate_random_password()
        
        # 4. Mở trang đăng ký
        url = "https://chatgpt.com/auth/login"
        print(f"🌐 Đang mở {url}...")
        with _timed_stage("open_page"):
            driver.get(url)
        _report("open_page")

        # Không chụp baseline mailbox ở đây vì nó thêm một lượt gọi API trước khi gửi OTP.
        # Luồng mới ưu tiên timestamp + fast path get_code/get_messages để tránh delay chết.
        baseline_message_ids = None
        
        # 5. Điền form đăng ký bằng email và mật khẩu
        with _timed_stage("fill_signup_form"):
            signup_ok = fill_signup_form(driver, email, password)
        if not signup_ok:
            print("❌ Điền form đăng ký thất bại")
            failure_reason = "Điền form đăng ký thất bại"
            return _return_result()
        _report("fill_form")
        
        # 6. Chờ email xác minh
        # Mốc này phải đặt ngay sau khi bấm tiếp tục sang màn OTP.
        # Email cũ trước mốc này sẽ bị bỏ qua khi đọc bằng get_messages_oauth2.
        otp_since_ts = time.time()
        used_otp_codes = set()
        otp_accepted = False
        profile_flow_failures = 0
        with _timed_stage("otp_total"):
            for otp_attempt in range(3):
                with _timed_stage(f"otp_fetch_{otp_attempt + 1}"):
                    verification_code = _wait_verification_code(
                        email_context,
                        timeout=OTP_WAIT_BEFORE_RESEND_SECONDS,
                        since_ts=otp_since_ts,
                        exclude_codes=used_otp_codes,
                        baseline_message_ids=baseline_message_ids,
                    )

                if not verification_code:
                    print(f"⚠️ Quá {OTP_WAIT_BEFORE_RESEND_SECONDS}s vẫn chưa có OTP, thử bấm Gửi lại email...")
                    if click_resend_email_button(driver, timeout=8):
                        otp_since_ts = time.time()
                        time.sleep(1.5)
                        continue
                    print("⚠️ Không bấm được Gửi lại email, dừng chờ OTP ở profile này")
                    break

                with _timed_stage(f"otp_submit_{otp_attempt + 1}"):
                    otp_result = enter_verification_code(driver, verification_code)
                if otp_result == "accepted":
                    otp_accepted = True
                    break
                if otp_result == "retry":
                    used_otp_codes.add(verification_code)
                    profile_flow_failures += 1
                if otp_result == "inline_retry":
                    profile_flow_failures += 1
                    if profile_flow_failures >= 2:
                        print("❌ Form inline mail/OTP/tên/tuổi lỗi 2 lần, chuyển profile mới")
                        failure_reason = "Form inline lỗi 2 lần, chuyển profile mới"
                        return _return_result()
                if otp_result == "profile_error":
                    profile_flow_failures += 1
                    if profile_flow_failures >= 2:
                        print("❌ Trang lỗi sau OTP 2 lần trong cùng profile, chuyển profile mới")
                        failure_reason = "Trang lỗi sau OTP 2 lần, chuyển profile mới"
                        return _return_result()
                if otp_result == "failed":
                    print("❌ Nhập mã xác minh thất bại")
                    failure_reason = "Nhập mã xác minh thất bại"
                    return _return_result()

                if profile_flow_failures >= 2:
                    print("❌ Profile lỗi lặp quá 2 lần, chuyển profile mới")
                    failure_reason = "Profile lỗi lặp quá 2 lần"
                    return _return_result()

                print(f"🔁 OTP/form chưa được chấp nhận, lấy lại OTP mới ({otp_attempt + 2}/3)...")
                if otp_result == "retry":
                    otp_since_ts = time.time()

        if not otp_accepted:
            print("❌ Không lấy được OTP hợp lệ, dừng đăng ký")
            failure_reason = "Không lấy được OTP hợp lệ"
            return _return_result()
        _report("enter_code")
        
        # 8. Điền thông tin cá nhân
        with _timed_stage("fill_profile_info"):
            profile_ok = fill_profile_info(driver)
        if not profile_ok:
            print("❌ Điền thông tin cá nhân thất bại")
            failure_reason = "Điền thông tin cá nhân thất bại"
            return _return_result()
        _report("fill_profile")
        
        # 9. Lưu thông tin tài khoản sau khi đăng ký thành công
        save_to_txt(email, password, "Đã đăng ký")
        
        # 10. Hoàn tất đăng ký
        print("\n" + "=" * 50)
        print("🎉 Đăng ký thành công!")
        print(f"   Email: {email}")
        print(f"   Mật khẩu: {password}")
        print("=" * 50)
        
        success = True
        _report("registered")
        if not PAYMENT_CHECKOUT_ENABLED:
            print("⏸️ Đã tạo tài khoản thành công; tạm bỏ qua bước thanh toán/trial theo cấu hình payment.checkout_enabled=false")
            update_account_status(email, "Đã đăng ký, chưa chạy trial")
            return _return_result()

        if PAYMENT_FLOW not in {"trial_free", "petrix", "checkout_api", "direct_checkout"}:
            print(f"⚠️ payment.flow={PAYMENT_FLOW!r} không còn phân nhánh riêng; dùng checkout API mới mặc định")

        print(f"⏳ Đã vào trang chủ ChatGPT, chờ ổn định {CHECKOUT_HOME_STABILIZE_SECONDS}s trước khi chạy checkout mới...")
        time.sleep(CHECKOUT_HOME_STABILIZE_SECONDS)
        print("🔗 Đã vào trang chủ ChatGPT, bắt đầu tạo checkout trial mới...")
        with _timed_stage("get_pay_link"):
            checkout_result = create_trial_checkout(
                driver,
                country_code="ID",
                currency="IDR",
                log_func=print,
            )
        _report("checkout_link")

        email_bundle = _email_context_bundle(email_context)
        if checkout_result.get("success"):
            checkout_url = str(checkout_result.get("checkout_url") or "").strip()
            trial_success = bool(checkout_url)
            failure_reason = ""
        else:
            checkout_url = ""
            trial_success = False
            failure_reason = str(checkout_result.get("failure_reason") or "Tạo checkout trial thất bại")

        if checkout_url:
            update_account_status(
                email,
                f"Trial checkout: {checkout_url}",
                metadata=email_bundle,
            )
            save_success_account(email_bundle, checkout_url)
            print("✅ Đã hoàn tất luồng checkout mới")
            print("✅ Output checkout trial:")
            print(checkout_url)
        else:
            update_account_status(
                email,
                f"Đăng ký xong nhưng tạo checkout trial thất bại: {failure_reason}",
                metadata=email_bundle,
            )
            print(f"❌ Không tạo được checkout trial mới: {failure_reason}")

        return _return_result()
        
    except InterruptedError:
        print("🛑 Tác vụ đã bị người dùng buộc dừng")
        if email: update_account_status(email, "Người dùng đã dừng")
        stopped = True
        return _return_result()

    except BrowserStartupError as e:
        print(f"❌ Khởi tạo trình duyệt/GPM thất bại: {e}")
        stopped = True
        return _return_result()
        
    except Exception as e:
        print(f"❌ Đã xảy ra lỗi: {e}")
        failure_reason = f"Lỗi: {str(e)[:80]}"
        # Dù có lỗi vẫn lưu thông tin tài khoản hiện có để tiện kiểm tra
        if email and password:
            update_account_status(email, f"Lỗi: {str(e)[:50]}")
    
    finally:
        total_elapsed = time.perf_counter() - stage_started_at
        timings["total"] = round(total_elapsed, 2)
        print(f"⏱️ Tổng thời gian account: {total_elapsed:.2f}s")
        if driver and not manual_checkout_ready:
            print("🔒 Đang đóng trình duyệt...")
            driver.quit()
        if mark_result and email_context and browser_ready and not stopped:
            mark_account_result(email_context, success, failure_reason)
    
    return _return_result()
    



def run_batch():
    """
    Đăng ký tài khoản hàng loạt.
    """
    print("\n" + "=" * 60)
    print(f"🚀 Bắt đầu đăng ký hàng loạt, số lượng mục tiêu: {TOTAL_ACCOUNTS}")
    print("=" * 60 + "\n")

    print("\n⚠️  Miễn trừ trách nhiệm: dự án này chỉ dùng cho học tập và nghiên cứu. Không dùng cho mục đích thương mại hoặc hành vi vi phạm.")
    print("⚠️  Người dùng tự chịu mọi hậu quả do sử dụng sai quy định.\n")
    time.sleep(2)
    
    success_count = 0
    fail_count = 0
    registered_accounts = []
    
    for i in range(TOTAL_ACCOUNTS):
        print("\n" + "#" * 60)
        print(f"📝 Đang đăng ký tài khoản thứ {i + 1}/{TOTAL_ACCOUNTS}")
        print("#" * 60 + "\n")
        
        email, password, success = register_one_account()

        if not email:
            print("⚠️ Không còn mail để đăng ký hoặc retry, dừng batch")
            break
        
        if success:
            success_count += 1
            registered_accounts.append((email, password))
        else:
            fail_count += 1
        
        # Hiển thị tiến độ
        print("\n" + "-" * 40)
        print(f"📊 Tiến độ hiện tại: {i + 1}/{TOTAL_ACCOUNTS}")
        print(f"   ✅ Thành công: {success_count}")
        print(f"   ❌ Thất bại: {fail_count}")
        print("-" * 40)
        
        # Nếu còn tài khoản tiếp theo, chờ một khoảng thời gian ngẫu nhiên
        if i < TOTAL_ACCOUNTS - 1:
            wait_time = random.randint(BATCH_INTERVAL_MIN, BATCH_INTERVAL_MAX)
            print(f"\n⏳ Chờ {wait_time} giây rồi tiếp tục đăng ký tài khoản tiếp theo...")
            time.sleep(wait_time)
    
    # Thống kê cuối cùng
    print("\n" + "=" * 60)
    print("🏁 Đăng ký hàng loạt hoàn tất")
    print("=" * 60)
    print(f"   Tổng cộng: {TOTAL_ACCOUNTS}")
    print(f"   ✅ Thành công: {success_count}")
    print(f"   ❌ Thất bại: {fail_count}")
    
    if registered_accounts:
        print("\n📋 Tài khoản đã đăng ký thành công:")
        for email, password in registered_accounts:
            print(f"   - {email}")
    
    print("=" * 60)


if __name__ == "__main__":
    run_batch()
