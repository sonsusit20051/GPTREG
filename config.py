"""
Module tải cấu hình
Tải cấu hình từ file config.yaml, hỗ trợ cập nhật động

Cách sử dụng:
    from config import cfg
    
    # Truy cập mục cấu hình
    total = cfg.registration.total_accounts
    email_accounts_file = cfg.email.accounts_file
    
    # Hoặc import hằng số trực tiếp để tương thích code cũ
    from config import TOTAL_ACCOUNTS, EMAIL_ACCOUNTS_FILE
"""

import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, Any

# Thử import yaml, nếu chưa cài thì hiển thị hướng dẫn
try:
    import yaml
except ImportError:
    print("❌ Thiếu dependency PyYAML, vui lòng cài trước:")
    print("   pip install pyyaml")
    sys.exit(1)


# ==============================================================
# Định nghĩa dataclass cấu hình
# ==============================================================

@dataclass
class RegistrationConfig:
    """Cấu hình đăng ký."""
    total_accounts: int = 1
    min_age: int = 26
    max_age: int = 40


@dataclass
class EmailConfig:
    """Cấu hình dịch vụ email."""
    mode: str = "hotmail"
    accounts_file: str = "hotmail_accounts.txt"
    api_url: str = "https://tools.dongvanfb.net/api/get_code_oauth2"
    messages_api_url: str = "https://tools.dongvanfb.net/api/get_messages_oauth2"
    type: str = "all"
    wait_timeout: int = 60
    poll_interval: int = 1
    initial_delay: int = 0
    fallback_enabled: bool = True
    fallback_after: int = 0
    request_timeout: int = 5


@dataclass
class BrowserConfig:
    """Cấu hình trình duyệt."""
    max_wait_time: int = 600
    short_wait_time: int = 120
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    gpm_enabled: bool = False
    gpm_api_url: str = "http://127.0.0.1:19995"
    gpm_profile_ids: list[str] = field(default_factory=list)
    gpm_start_endpoint: str = "/api/v1/profiles/start/{profile_id}"
    gpm_auto_create: bool = False
    gpm_auto_profile_prefix: str = "chatgpt-auto"
    gpm_group_name: str = "All"
    gpm_browser_version: str = "auto"
    gpm_os_type: int = 3
    gpm_os: str = "Windows 11"
    gpm_raw_proxy: str = ""
    gpm_delete_created_on_close: bool = True
    background_mode: bool = False
    offscreen_x: int = -10000
    offscreen_y: int = -10000
    visible_grid_enabled: bool = False
    visible_grid_cols: int = 2
    visible_grid_rows: int = 2
    visible_window_width: int = 720
    visible_window_height: int = 450
    visible_start_x: int = 0
    visible_start_y: int = 0


@dataclass
class PasswordConfig:
    """Cấu hình mật khẩu."""
    length: int = 16
    charset: str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%"


@dataclass
class RetryConfig:
    """Cấu hình retry."""
    http_max_retries: int = 5
    http_timeout: int = 30
    error_page_max_retries: int = 5
    button_click_max_retries: int = 3


@dataclass
class BatchConfig:
    """Cấu hình đăng ký hàng loạt."""
    interval_min: int = 5
    interval_max: int = 15


@dataclass
class FilesConfig:
    """Cấu hình đường dẫn file."""
    accounts_file: str = "registered_accounts.txt"


@dataclass
class CreditCardConfig:
    """Cấu hình thẻ tín dụng."""
    number: str = ""
    expiry: str = ""
    expiry_month: str = ""
    expiry_year: str = ""
    cvc: str = ""


@dataclass
class PaymentConfig:
    """Cấu hình thanh toán."""
    checkout_enabled: bool = True
    flow: str = "trial_free"
    credit_card: CreditCardConfig = field(default_factory=CreditCardConfig)


@dataclass
class AppConfig:
    """Cấu hình đầy đủ của ứng dụng."""
    registration: RegistrationConfig = field(default_factory=RegistrationConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    password: PasswordConfig = field(default_factory=PasswordConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
    payment: PaymentConfig = field(default_factory=PaymentConfig)


# ==============================================================
# Bộ tải cấu hình
# ==============================================================

class ConfigLoader:
    """
    Bộ tải cấu hình.
    Hỗ trợ tải cấu hình từ file YAML và gộp với giá trị mặc định.
    """
    
    # Đường dẫn tìm file cấu hình, xếp theo độ ưu tiên
    CONFIG_FILES = [
        "config.yaml",
        "config.yml",
        "config.local.yaml",
        "config.local.yml",
    ]
    
    def __init__(self, config_path: Optional[str] = None):
        """
        Khởi tạo bộ tải cấu hình.
        
        Tham số:
            config_path: đường dẫn file cấu hình chỉ định, nếu None thì tự tìm
        """
        self.config_path = config_path
        self.raw_config: Dict[str, Any] = {}
        self.config = AppConfig()
        
        self._load_config()
    
    def _find_config_file(self) -> Optional[Path]:
        """Tìm file cấu hình."""
        # Lấy thư mục chứa script
        base_dir = Path(__file__).parent
        
        for filename in self.CONFIG_FILES:
            config_file = base_dir / filename
            if config_file.exists():
                return config_file
        
        return None
    
    def _load_config(self) -> None:
        """Tải file cấu hình."""
        if self.config_path:
            config_file = Path(self.config_path)
        else:
            config_file = self._find_config_file()
        
        if config_file is None or not config_file.exists():
            print("⚠️ Không tìm thấy file cấu hình config.yaml")
            print("   Vui lòng sao chép config.example.yaml thành config.yaml rồi chỉnh cấu hình")
            print("   Tiếp tục chạy với cấu hình mặc định...")
            return
        
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                self.raw_config = yaml.safe_load(f) or {}
            
            self.config_path = str(config_file)
            print(f"📄 Đã tải file cấu hình: {config_file.name}")
            
            # Phân tích cấu hình vào dataclass
            self._parse_config()
            
        except yaml.YAMLError as e:
            print(f"❌ File cấu hình sai định dạng: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Tải file cấu hình thất bại: {e}")
            sys.exit(1)
    
    def _parse_config(self) -> None:
        """Phân tích cấu hình thô vào dataclass."""
        # Cấu hình đăng ký
        if 'registration' in self.raw_config:
            reg = self.raw_config['registration']
            self.config.registration = RegistrationConfig(
                total_accounts=reg.get('total_accounts', 1),
                min_age=reg.get('min_age', 26),
                max_age=reg.get('max_age', 40)
            )
        
        # Cấu hình email
        if 'email' in self.raw_config:
            email = self.raw_config['email']
            self.config.email = EmailConfig(
                mode=email.get('mode', 'hotmail'),
                accounts_file=email.get('accounts_file', 'hotmail_accounts.txt'),
                api_url=email.get('api_url', 'https://tools.dongvanfb.net/api/get_code_oauth2'),
                messages_api_url=email.get('messages_api_url', 'https://tools.dongvanfb.net/api/get_messages_oauth2'),
                type=email.get('type', 'all'),
                wait_timeout=email.get('wait_timeout', 60),
                poll_interval=email.get('poll_interval', 1),
                initial_delay=email.get('initial_delay', 0),
                fallback_enabled=email.get('fallback_enabled', True),
                fallback_after=email.get('fallback_after', 0),
                request_timeout=email.get('request_timeout', 5),
            )
        
        # Cấu hình trình duyệt
        if 'browser' in self.raw_config:
            browser = self.raw_config['browser']
            gpm_profile_ids = browser.get('gpm_profile_ids', [])
            if isinstance(gpm_profile_ids, str):
                gpm_profile_ids = [item.strip() for item in gpm_profile_ids.split(",") if item.strip()]
            self.config.browser = BrowserConfig(
                max_wait_time=browser.get('max_wait_time', 600),
                short_wait_time=browser.get('short_wait_time', 120),
                user_agent=browser.get('user_agent', ''),
                gpm_enabled=browser.get('gpm_enabled', False),
                gpm_api_url=browser.get('gpm_api_url', 'http://127.0.0.1:19995'),
                gpm_profile_ids=gpm_profile_ids,
                gpm_start_endpoint=browser.get('gpm_start_endpoint', '/api/v1/profiles/start/{profile_id}'),
                gpm_auto_create=browser.get('gpm_auto_create', False),
                gpm_auto_profile_prefix=browser.get('gpm_auto_profile_prefix', 'chatgpt-auto'),
                gpm_group_name=browser.get('gpm_group_name', 'All'),
                gpm_browser_version=browser.get('gpm_browser_version', 'auto'),
                gpm_os_type=browser.get('gpm_os_type', 3),
                gpm_os=browser.get('gpm_os', 'Windows 11'),
                gpm_raw_proxy=browser.get('gpm_raw_proxy', ''),
                gpm_delete_created_on_close=browser.get('gpm_delete_created_on_close', True),
                background_mode=browser.get('background_mode', False),
                offscreen_x=browser.get('offscreen_x', -10000),
                offscreen_y=browser.get('offscreen_y', -10000),
                visible_grid_enabled=browser.get('visible_grid_enabled', False),
                visible_grid_cols=browser.get('visible_grid_cols', 2),
                visible_grid_rows=browser.get('visible_grid_rows', 2),
                visible_window_width=browser.get('visible_window_width', 720),
                visible_window_height=browser.get('visible_window_height', 450),
                visible_start_x=browser.get('visible_start_x', 0),
                visible_start_y=browser.get('visible_start_y', 0),
            )
        
        # Cấu hình mật khẩu
        if 'password' in self.raw_config:
            pwd = self.raw_config['password']
            self.config.password = PasswordConfig(
                length=pwd.get('length', 16),
                charset=pwd.get('charset', '')
            )
        
        # Cấu hình retry
        if 'retry' in self.raw_config:
            retry = self.raw_config['retry']
            self.config.retry = RetryConfig(
                http_max_retries=retry.get('http_max_retries', 5),
                http_timeout=retry.get('http_timeout', 30),
                error_page_max_retries=retry.get('error_page_max_retries', 5),
                button_click_max_retries=retry.get('button_click_max_retries', 3)
            )
        
        # Cấu hình hàng loạt
        if 'batch' in self.raw_config:
            batch = self.raw_config['batch']
            self.config.batch = BatchConfig(
                interval_min=batch.get('interval_min', 5),
                interval_max=batch.get('interval_max', 15)
            )
        
        # Cấu hình file
        if 'files' in self.raw_config:
            files = self.raw_config['files']
            self.config.files = FilesConfig(
                accounts_file=files.get('accounts_file', 'registered_accounts.txt')
            )
        
        # Cấu hình thanh toán
        if 'payment' in self.raw_config:
            payment = self.raw_config['payment']
            self.config.payment = PaymentConfig(
                checkout_enabled=payment.get('checkout_enabled', True),
                flow=payment.get('flow', 'trial_free'),
                credit_card=CreditCardConfig(
                    number=payment.get('credit_card', {}).get('number', ''),
                    expiry=payment.get('credit_card', {}).get('expiry', ''),
                    expiry_month=payment.get('credit_card', {}).get('expiry_month', ''),
                    expiry_year=payment.get('credit_card', {}).get('expiry_year', ''),
                    cvc=payment.get('credit_card', {}).get('cvc', '')
                )
            )
    
    def reload(self) -> None:
        """Tải lại file cấu hình."""
        self._load_config()
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        Lấy giá trị cấu hình thô, hỗ trợ đường dẫn bằng dấu chấm.
        
        Tham số:
            key: khóa cấu hình, hỗ trợ đường dẫn phân tách bằng dấu chấm như 'email.accounts_file'
            default: giá trị mặc định
        
        Trả về:
            giá trị cấu hình hoặc giá trị mặc định
        """
        keys = key.split('.')
        value = self.raw_config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value


# ==============================================================
# Instance cấu hình toàn cục
# ==============================================================

# Tạo bộ tải cấu hình toàn cục
_loader = ConfigLoader()

# Đối tượng cấu hình, khuyến nghị dùng
cfg = _loader.config


# ==============================================================
# Export tương thích để giữ code cũ chạy được
# ==============================================================

# Cấu hình đăng ký
TOTAL_ACCOUNTS = cfg.registration.total_accounts
MIN_AGE = cfg.registration.min_age
MAX_AGE = cfg.registration.max_age

# Cấu hình email
EMAIL_MODE = cfg.email.mode
EMAIL_ACCOUNTS_FILE = cfg.email.accounts_file
EMAIL_API_URL = cfg.email.api_url
EMAIL_MESSAGES_API_URL = cfg.email.messages_api_url
EMAIL_API_TYPE = cfg.email.type
EMAIL_WAIT_TIMEOUT = cfg.email.wait_timeout
EMAIL_POLL_INTERVAL = cfg.email.poll_interval
EMAIL_INITIAL_DELAY = cfg.email.initial_delay
EMAIL_FALLBACK_ENABLED = cfg.email.fallback_enabled
EMAIL_FALLBACK_AFTER = cfg.email.fallback_after
EMAIL_REQUEST_TIMEOUT = cfg.email.request_timeout

# Cấu hình trình duyệt
MAX_WAIT_TIME = cfg.browser.max_wait_time
SHORT_WAIT_TIME = cfg.browser.short_wait_time
USER_AGENT = cfg.browser.user_agent
GPM_ENABLED = cfg.browser.gpm_enabled
GPM_API_URL = cfg.browser.gpm_api_url
GPM_PROFILE_IDS = cfg.browser.gpm_profile_ids
GPM_START_ENDPOINT = cfg.browser.gpm_start_endpoint
GPM_AUTO_CREATE = cfg.browser.gpm_auto_create
GPM_AUTO_PROFILE_PREFIX = cfg.browser.gpm_auto_profile_prefix
GPM_GROUP_NAME = cfg.browser.gpm_group_name
GPM_BROWSER_VERSION = cfg.browser.gpm_browser_version
GPM_OS_TYPE = cfg.browser.gpm_os_type
GPM_OS = cfg.browser.gpm_os
GPM_RAW_PROXY = cfg.browser.gpm_raw_proxy
GPM_DELETE_CREATED_ON_CLOSE = cfg.browser.gpm_delete_created_on_close
BACKGROUND_MODE = cfg.browser.background_mode
OFFSCREEN_X = cfg.browser.offscreen_x
OFFSCREEN_Y = cfg.browser.offscreen_y
VISIBLE_GRID_ENABLED = cfg.browser.visible_grid_enabled
VISIBLE_GRID_COLS = cfg.browser.visible_grid_cols
VISIBLE_GRID_ROWS = cfg.browser.visible_grid_rows
VISIBLE_WINDOW_WIDTH = cfg.browser.visible_window_width
VISIBLE_WINDOW_HEIGHT = cfg.browser.visible_window_height
VISIBLE_START_X = cfg.browser.visible_start_x
VISIBLE_START_Y = cfg.browser.visible_start_y

# Cấu hình mật khẩu
PASSWORD_LENGTH = cfg.password.length
PASSWORD_CHARS = cfg.password.charset

# Cấu hình retry
HTTP_MAX_RETRIES = cfg.retry.http_max_retries
HTTP_TIMEOUT = cfg.retry.http_timeout
ERROR_PAGE_MAX_RETRIES = cfg.retry.error_page_max_retries
BUTTON_CLICK_MAX_RETRIES = cfg.retry.button_click_max_retries

# Cấu hình hàng loạt
BATCH_INTERVAL_MIN = cfg.batch.interval_min
BATCH_INTERVAL_MAX = cfg.batch.interval_max

# Cấu hình file
TXT_FILE = cfg.files.accounts_file

# Cấu hình thanh toán dạng dict để tương thích code cũ
CREDIT_CARD_INFO = {
    "number": cfg.payment.credit_card.number,
    "expiry": cfg.payment.credit_card.expiry,
    "expiry_month": cfg.payment.credit_card.expiry_month,
    "expiry_year": cfg.payment.credit_card.expiry_year,
    "cvc": cfg.payment.credit_card.cvc
}
PAYMENT_CHECKOUT_ENABLED = cfg.payment.checkout_enabled
PAYMENT_FLOW = cfg.payment.flow


# ==============================================================
# Hàm tiện ích
# ==============================================================

def reload_config() -> None:
    """
    Tải lại file cấu hình.
    Lưu ý: thao tác này không cập nhật các hằng số đã import, chỉ cập nhật đối tượng cfg.
    """
    global cfg
    _loader.reload()
    cfg = _loader.config


def get_config() -> AppConfig:
    """Lấy đối tượng cấu hình hiện tại."""
    return cfg


def print_config_summary() -> None:
    """In tóm tắt cấu hình."""
    print("\n" + "=" * 50)
    print("📋 Tóm tắt cấu hình hiện tại")
    print("=" * 50)
    print(f"  Số lượng tài khoản đăng ký: {cfg.registration.total_accounts}")
    print(f"  Chế độ email: {cfg.email.mode}")
    print(f"  File Hotmail: {cfg.email.accounts_file}")
    print(f"  API lấy mã: {cfg.email.api_url}")
    print(f"  File lưu tài khoản: {cfg.files.accounts_file}")
    print(f"  Khoảng cách hàng loạt: {cfg.batch.interval_min}-{cfg.batch.interval_max} giây")
    print("=" * 50 + "\n")


# In thông tin cấu hình khi chạy trực tiếp module này
if __name__ == "__main__":
    print_config_summary()
