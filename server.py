import logging
import threading
import time
import queue
import builtins
import os
import random
import socket
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory

APP_VERSION = "2026.05.03-gpm-cleanup-v31"
STARTED_AT = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
MAX_PARALLEL_REGISTRATIONS = 2

# Import logic nghiệp vụ
import main
import browser
import email_service
from config import cfg
from utils import SUCCESS_ACCOUNTS_FILE

app = Flask(__name__, static_url_path='')

# ==========================================
# 🔧 Quản lý trạng thái và bắt nhật ký
# ==========================================

# ==========================================
# 🔧 Quản lý trạng thái và bắt nhật ký
# ==========================================

# Trạng thái toàn cục
class AppState:
    def __init__(self):
        self.is_running = False
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.current_action = "Đang chờ bắt đầu"
        self.logs = []
        self.lock = threading.Lock()
        self.current_drivers = {}
        self.driver_lock = threading.Lock()
        
        # Bộ đệm luồng MJPEG
        self.last_frame = None 
        self.frame_lock = threading.Lock()

    def add_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.append(f"[{timestamp}] {message}")
            if len(self.logs) > 1000:
                self.logs.pop(0)

    def get_logs(self, start_index=0):
        with self.lock:
            return list(self.logs[start_index:])
            
    def update_frame(self, frame_bytes):
        with self.frame_lock:
            self.last_frame = frame_bytes
            
    def get_frame(self):
        with self.frame_lock:
            return self.last_frame

    def set_current_driver(self, driver):
        with self.driver_lock:
            self.current_drivers[threading.get_ident()] = driver

    def clear_current_driver(self, driver=None):
        with self.driver_lock:
            if driver is None:
                self.current_drivers.pop(threading.get_ident(), None)
            else:
                for thread_id, current_driver in list(self.current_drivers.items()):
                    if current_driver is driver:
                        self.current_drivers.pop(thread_id, None)

    def close_current_driver(self):
        with self.driver_lock:
            drivers = list(self.current_drivers.values())
            self.current_drivers.clear()

        if not drivers:
            return False

        closed_any = False
        for driver in drivers:
            try:
                driver.quit()
                closed_any = True
            except Exception as e:
                main.print(f"⚠️ Đóng trình duyệt khi dừng thất bại: {e}")
        return closed_any

state = AppState()


def find_available_port(preferred_port, max_tries=20):
    """Return preferred_port if free, otherwise the next available port."""
    for port in range(preferred_port, preferred_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                continue
            return port
    raise OSError(f"No available port found from {preferred_port} to {preferred_port + max_tries - 1}")

# Hack: chặn hàm print để bắt nhật ký
original_print = builtins.print
def hooked_print(*args, **kwargs):
    sep = kwargs.get('sep', ' ')
    msg = sep.join(map(str, args))
    state.add_log(msg)
    original_print(*args, **kwargs)

# Áp dụng chặn print
main.print = hooked_print
browser.print = hooked_print
email_service.print = hooked_print

# ==========================================
# 🧵 Luồng chạy nền
# ==========================================
def worker_thread(count):
    target_success_count = max(1, int(count or 1))
    worker_count = min(MAX_PARALLEL_REGISTRATIONS, target_success_count)
    attempts_started = 0
    active_slots = 0

    state.is_running = True
    state.stop_requested = False
    state.success_count = 0
    state.fail_count = 0
    state.current_action = f"🚀 Tác vụ bắt đầu: cần {target_success_count} tài khoản, chạy song song {worker_count} luồng"
    email_service.reset_runtime_state(clear_failures=True)
    
    # Xóa hình ảnh vòng trước để tránh hiển thị sót khung hình
    state.update_frame(None)
    
    main.print(
        f"🚀 Bắt đầu tác vụ: mục tiêu hoàn thành {target_success_count} tài khoản, "
        f"chạy tối đa {worker_count} trình duyệt song song"
    )
    
    try:
        def monitor(driver, step):
            state.set_current_driver(driver)

            # 1. Kiểm tra yêu cầu dừng
            if state.stop_requested:
                main.print("🛑 Phát hiện yêu cầu dừng, đang ngắt tác vụ...")
                raise InterruptedError("Người dùng yêu cầu dừng")

            if step == "init_browser":
                state.current_action = "Trình duyệt đã khởi tạo"
                return
            
            # 2. Chụp màn hình để cập nhật luồng MJPEG
            try:
                # Lấy byte PNG trong bộ nhớ
                png_bytes = driver.get_screenshot_as_png()
                state.update_frame(png_bytes)
            except Exception as e:
                main.print(f"⚠️ Cập nhật luồng ảnh chụp thất bại: {e}")

        def run_worker(worker_index):
            nonlocal attempts_started, active_slots

            while not state.stop_requested:
                with state.lock:
                    if state.success_count + active_slots >= target_success_count:
                        break
                    attempts_started += 1
                    active_slots += 1
                    attempt_no = attempts_started
                    current_success = state.success_count
                    current_fail = state.fail_count

                state.current_action = (
                    f"Đang chạy {active_slots}/{worker_count} luồng, "
                    f"thành công {current_success}/{target_success_count}, lỗi {current_fail}"
                )
                main.print(f"🧵 Worker {worker_index} bắt đầu account #{attempt_no}")

                try:
                    email, password, success = main.register_one_account(monitor_callback=monitor)
                    state.clear_current_driver()

                    with state.lock:
                        active_slots -= 1
                        no_more_accounts = not email and not success
                        if success:
                            state.success_count += 1
                        elif not no_more_accounts:
                            state.fail_count += 1
                        current_success = state.success_count
                        current_fail = state.fail_count

                    if no_more_accounts:
                        main.print(f"⚠️ Worker {worker_index}: không còn mail để đăng ký hoặc retry")
                        break
                    else:
                        main.print(
                            f"🧵 Worker {worker_index} kết thúc account #{attempt_no}: "
                            f"{'thành công' if success else 'thất bại'} "
                            f"({current_success}/{target_success_count} thành công, {current_fail} lỗi)"
                        )
                except InterruptedError:
                    state.clear_current_driver()
                    with state.lock:
                        active_slots = max(0, active_slots - 1)
                    main.print(f"🛑 Worker {worker_index} đã bị ngắt")
                    break
                except Exception as e:
                    state.clear_current_driver()
                    with state.lock:
                        active_slots = max(0, active_slots - 1)
                        state.fail_count += 1
                        current_fail = state.fail_count
                    main.print(f"❌ Worker {worker_index} account #{attempt_no} ngoại lệ: {str(e)}")

                with state.lock:
                    if state.success_count >= target_success_count:
                        break

        threads = []
        for i in range(worker_count):
            if state.stop_requested:
                break
            thread = threading.Thread(target=run_worker, args=(i + 1,), daemon=True)
            thread.start()
            threads.append(thread)

        for thread in threads:
            while thread.is_alive():
                if state.stop_requested:
                    break
                thread.join(timeout=0.5)

        for thread in threads:
            thread.join(timeout=5)
                    
    except Exception as e:
        main.print(f"💥 Lỗi nghiêm trọng: {e}")
    finally:
        state.clear_current_driver()
        state.is_running = False
        state.current_action = "Tác vụ đã hoàn tất"
        main.print("🏁 Tác vụ kết thúc")

# ==========================================
# 🌊 Bộ sinh luồng MJPEG
# ==========================================
def gen_frames():
    """Bộ sinh dữ liệu luồng."""
    while True:
        frame = state.get_frame()
        if frame:
            yield (b'--frame\r\n'
                   b'Content-Type: image/png\r\n\r\n' + frame + b'\r\n')
        else:
            # Nếu chưa có hình ảnh, ví dụ vừa khởi động, chỉ cần chờ
            pass
            
        time.sleep(0.5) # Kiểm soát tần suất làm mới để tránh trình duyệt request quá dày

@app.route('/video_feed')
def video_feed():
    return Flask.response_class(gen_frames(),
                               mimetype='multipart/x-mixed-replace; boundary=frame')

# ==========================================
# 🌐 API endpoint
# ==========================================

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/status')
def get_status():
    # Lấy số lượng tài khoản đã lưu
    inventory_emails = set()
    success_file = os.path.join(os.path.dirname(__file__), SUCCESS_ACCOUNTS_FILE)
    if os.path.exists(success_file):
        try:
            with open(success_file, 'r', encoding='utf-8') as f:
                for line in f:
                    parts = line.rstrip('\n').split('----')
                    if len(parts) >= 3 and 'http' in parts[2]:
                        email = parts[1].split('|', 1)[0].strip().lower()
                        if email:
                            inventory_emails.add(email)
        except:
            pass
    if os.path.exists(cfg.files.accounts_file):
        try:
            with open(cfg.files.accounts_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if 'Link 0 IDR:' not in line:
                        continue
                    email = line.split('----', 1)[0].strip().lower()
                    if email:
                        inventory_emails.add(email)
        except:
            pass
    total_inventory = len(inventory_emails)

    return jsonify({
        "version": APP_VERSION,
        "started_at": STARTED_AT,
        "is_running": state.is_running,
        "current_action": state.current_action,
        "success": state.success_count,
        "fail": state.fail_count,
        "total_inventory": total_inventory,
        "logs": state.get_logs(int(request.args.get('log_index', 0)))
    })

@app.route('/api/start', methods=['POST'])
def start_task():
    if state.is_running:
        return jsonify({"error": "Already running"}), 400
    
    data = request.json
    count = data.get('count', 1)
    
    threading.Thread(target=worker_thread, args=(count,), daemon=True).start()
    return jsonify({"status": "started"})

@app.route('/api/stop', methods=['POST'])
def stop_task():
    if not state.is_running:
        return jsonify({"error": "Not running"}), 400
    
    state.stop_requested = True
    state.current_action = "Đang dừng tác vụ..."
    closed = state.close_current_driver()
    if closed:
        main.print("🛑 Đã gửi lệnh dừng và đóng trình duyệt hiện tại")
    else:
        main.print("🛑 Đã gửi lệnh dừng, đang chờ tác vụ tới điểm kiểm tra tiếp theo")
    return jsonify({"status": "stopping", "browser_closed": closed})

@app.route('/api/accounts')
def get_accounts():
    accounts = []
    seen_emails = set()
    hotmail_map = {}

    hotmail_file = cfg.email.accounts_file
    if not os.path.isabs(hotmail_file):
        hotmail_file = os.path.join(os.path.dirname(__file__), hotmail_file)

    if os.path.exists(hotmail_file):
        try:
            with open(hotmail_file, 'r', encoding='utf-8') as f:
                for raw_line in f:
                    bundle = raw_line.strip()
                    if not bundle or bundle.startswith("#"):
                        continue
                    email = bundle.split('|', 1)[0].strip()
                    if email:
                        hotmail_map[email.lower()] = bundle
        except Exception as e:
            main.print(f"⚠️ Không đọc được hotmail_accounts: {e}")

    success_file = os.path.join(os.path.dirname(__file__), SUCCESS_ACCOUNTS_FILE)
    if os.path.exists(success_file):
        try:
            with open(success_file, 'r', encoding='utf-8') as f:
                for line in f:
                    parts = line.rstrip('\n').split('----')
                    if len(parts) < 3:
                        continue
                    bundle = parts[1].strip()
                    pay_link = parts[2].strip()
                    email = bundle.split('|', 1)[0].strip()
                    if not email or not pay_link:
                        continue
                    seen_emails.add(email.lower())
                    accounts.append({
                        "email": email,
                        "email_bundle": bundle,
                        "pay_link": pay_link,
                        "time": parts[0].strip(),
                    })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    if os.path.exists(cfg.files.accounts_file):
        try:
            with open(cfg.files.accounts_file, 'r', encoding='utf-8') as f:
                for line in f:
                    parts = line.strip().split('----')
                    if len(parts) >= 4:
                        email = parts[0].strip()
                        status = parts[3].strip()
                        if "Link 0 IDR:" not in status:
                            continue
                        if email.lower() in seen_emails:
                            continue
                        pay_link = status.split("Link 0 IDR:", 1)[1].strip()
                        stored_bundle = "----".join(parts[4:]).strip() if len(parts) > 4 else ""
                        seen_emails.add(email.lower())
                        accounts.append({
                            "email": email,
                            "email_bundle": stored_bundle or hotmail_map.get(email.lower(), email),
                            "pay_link": pay_link,
                            "time": parts[2].strip(),
                        })
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    # Đảo danh sách để bản ghi mới nhất nằm trên đầu
    return jsonify(accounts[::-1])

if __name__ == '__main__':
    from waitress import serve
    preferred_port = int(os.environ.get("PORT", "5050"))
    port = find_available_port(preferred_port)
    print(f"🧩 Backend version: {APP_VERSION} | started_at: {STARTED_AT}")
    if port != preferred_port:
        print(f"⚠️ Port {preferred_port} đang bận, tự chuyển sang port {port}")
    print(f"🌐 Web Server started at http://localhost:{port}")
    # Dùng server Waitress cho môi trường chạy ổn định hơn
    # threads=6 hỗ trợ đồng thời: frontend, thăm dò API, luồng MJPEG và tác vụ nền
    serve(app, host='0.0.0.0', port=port, threads=6)
