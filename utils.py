"""
Module hàm tiện ích
Chứa các hàm phụ trợ dùng chung
"""

import random
import string
import csv
import os
import re
import time
import threading
from datetime import datetime
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    PASSWORD_LENGTH,
    PASSWORD_CHARS,
    PASSWORD_CHARS,
    TXT_FILE,
    HTTP_MAX_RETRIES,
    HTTP_MAX_RETRIES,
    HTTP_TIMEOUT,
    USER_AGENT,
    MIN_AGE,
    MAX_AGE
)

SUCCESS_ACCOUNTS_FILE = "successful_accounts.txt"
_success_accounts_lock = threading.Lock()
MIN_BIRTH_YEAR = 2000

# Thử import thư viện Faker
try:
    from faker import Faker
    # Tạo Faker đa ngôn ngữ, ưu tiên tiếng Anh để dữ liệu tự nhiên hơn
    fake = Faker(['en_US', 'en_GB'])
    # Đặt seed để có thể tái lập kết quả nếu cần
    # Faker.seed(0)
    FAKER_AVAILABLE = True
    print("✅ Đã tải thư viện Faker, sẽ dùng dữ liệu giả tự nhiên hơn")
except ImportError:
    FAKER_AVAILABLE = False
    print("⚠️ Chưa cài thư viện Faker, sẽ dùng danh sách tên tích hợp")
    print("   Lệnh cài đặt: pip install Faker")

# ============================================================
# Danh sách tên tiếng Anh phổ biến để tạo tên người dùng ngẫu nhiên
# ============================================================

FIRST_NAMES = [
    # Tên nam
    "James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph",
    "Thomas", "Charles", "Christopher", "Daniel", "Matthew", "Anthony", "Mark",
    "Donald", "Steven", "Paul", "Andrew", "Joshua", "Kenneth", "Kevin", "Brian",
    "George", "Timothy", "Ronald", "Edward", "Jason", "Jeffrey", "Ryan",
    # Tên nữ
    "Mary", "Patricia", "Jennifer", "Linda", "Barbara", "Elizabeth", "Susan",
    "Jessica", "Sarah", "Karen", "Lisa", "Nancy", "Betty", "Margaret", "Sandra",
    "Ashley", "Kimberly", "Emily", "Donna", "Michelle", "Dorothy", "Carol",
    "Amanda", "Melissa", "Deborah", "Stephanie", "Rebecca", "Sharon", "Laura", "Cynthia"
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
    "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen",
    "Hill", "Flores", "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell"
]


def create_http_session():
    """
    Tạo HTTP Session có cơ chế retry.
    
    Trả về:
        requests.Session: đối tượng Session đã cấu hình retry
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=HTTP_MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST", "OPTIONS"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# Tạo HTTP Session toàn cục
http_session = create_http_session()


def get_user_agent():
    """
    Lấy chuỗi User-Agent.
    
    Trả về:
        str: User-Agent
    """
    return USER_AGENT


def generate_random_password(length=None):
    """
    Tạo mật khẩu ngẫu nhiên.
    Đảm bảo mật khẩu có chữ hoa, chữ thường, chữ số và ký tự đặc biệt.
    
    Tham số:
        length: độ dài mật khẩu, mặc định lấy từ file cấu hình
    
    Trả về:
        str: mật khẩu đã tạo
    """
    if length is None:
        length = PASSWORD_LENGTH
    
    # Tạo mật khẩu ngẫu nhiên với độ dài chỉ định trước
    password = ''.join(random.choice(PASSWORD_CHARS) for _ in range(length))
    
    # Đảm bảo có đủ loại ký tự bằng cách thay 4 ký tự đầu
    password = (
        random.choice(string.ascii_uppercase) +   # Chữ hoa
        random.choice(string.ascii_lowercase) +   # Chữ thường
        random.choice(string.digits) +            # Chữ số
        random.choice("!@#$%") +                  # Ký tự đặc biệt
        password[4:]                              # Phần còn lại
    )
    
    print(f"✅ Đã tạo mật khẩu: {password}")
    return password


def save_to_txt(email: str, password: str = None, status="Đã đăng ký", metadata: str = ""):
    """
    Lưu thông tin tài khoản vào file TXT, định dạng: email----mật khẩu----thời gian----trạng thái.
    Nếu tài khoản đã tồn tại thì cập nhật thông tin.
    """
    try:
        file_path = os.path.join(os.path.dirname(__file__), TXT_FILE)
        current_date = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Đọc nội dung hiện có
        lines = []
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        
        # Kiểm tra đã tồn tại chưa, nếu có thì cập nhật
        found = False
        metadata_part = f"----{metadata}" if metadata else ""
        new_line_content = f"{email}----{password if password else 'N/A'}----{current_date}----{status}{metadata_part}\n"
        
        for i, line in enumerate(lines):
            # Chỉ khớp email ở đầu dòng để tránh nhầm với mật khẩu hoặc trạng thái
            if line.startswith(f"{email}----"):
                parts = line.strip().split("----")
                current_password_in_file = parts[1] if len(parts) > 1 else 'N/A'
                
                # Nếu có mật khẩu mới thì dùng mật khẩu mới, nếu không giữ mật khẩu cũ
                final_password = password if password else current_password_in_file
                final_metadata = metadata
                if not final_metadata and len(parts) > 4:
                    final_metadata = "----".join(parts[4:])
                metadata_part = f"----{final_metadata}" if final_metadata else ""
                lines[i] = f"{email}----{final_password}----{current_date}----{status}{metadata_part}\n"
                found = True
                break
        
        if not found:
            lines.append(new_line_content)
            
        # Ghi lại file
        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
            
        print(f"💾 Đã cập nhật trạng thái tài khoản: {status}")
        
    except Exception as e:
        print(f"❌ Lưu/cập nhật thông tin tài khoản thất bại: {e}")

def update_account_status(email: str, new_status: str, password: str = None, metadata: str = ""):
    """
    Hàm tiện ích chuyên cập nhật trạng thái tài khoản.
    
    Tham số:
        email: địa chỉ email
        new_status: chuỗi trạng thái mới
        password: truyền mật khẩu mới nếu cần cập nhật, nếu không để None
    """
    save_to_txt(email, password, new_status, metadata=metadata)


def save_success_account(email_bundle: str, pay_link: str):
    """
    Lưu riêng account đã hoàn tất toàn bộ flow.
    Format: time----mail|pass|refresh_token|client_id----pay_link
    File này phục vụ bảng quản lý và backup nếu browser/web crash.
    """
    if not email_bundle or not pay_link:
        print("⚠️ Thiếu cụm mail hoặc link pay, bỏ qua lưu successful_accounts.txt")
        return

    email = email_bundle.split("|", 1)[0].strip().lower()
    if not email:
        print("⚠️ Cụm mail không hợp lệ, bỏ qua lưu successful_accounts.txt")
        return

    file_path = os.path.join(os.path.dirname(__file__), SUCCESS_ACCOUNTS_FILE)
    current_date = datetime.now().strftime("%Y%m%d_%H%M%S")
    new_line = f"{current_date}----{email_bundle}----{pay_link}\n"

    with _success_accounts_lock:
        try:
            lines = []
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

            updated = False
            for i, line in enumerate(lines):
                parts = line.rstrip("\n").split("----")
                if len(parts) < 3:
                    continue
                existing_email = parts[1].split("|", 1)[0].strip().lower()
                if existing_email == email:
                    lines[i] = new_line
                    updated = True
                    break

            if not updated:
                lines.append(new_line)

            with open(file_path, "w", encoding="utf-8") as f:
                f.writelines(lines)

            print(f"💾 Đã lưu account hoàn tất vào {SUCCESS_ACCOUNTS_FILE}")
        except Exception as e:
            print(f"❌ Lưu {SUCCESS_ACCOUNTS_FILE} thất bại: {e}")


def extract_verification_code(content: str):
    """
    Trích xuất mã xác minh 6 chữ số từ nội dung email.
    
    Tham số:
        content: nội dung email HTML hoặc text thuần
    
    Trả về:
        str: mã xác minh trích xuất được, không tìm thấy trả về None
    """
    if not content:
        return None
    
    # Mẫu khớp mã xác minh, xếp theo độ ưu tiên
    patterns = [
        r'code is\s*(\d{6})',             # Định dạng tiếng Anh
        r'verification code[:\s]*(\d{6})',  # Định dạng tiếng Anh đầy đủ
        r'(\d{6})',                       # 6 chữ số bất kỳ
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        if matches:
            code = matches[0]
            print(f"  ✅ Đã trích xuất mã xác minh: {code}")
            return code
    
    return None


def generate_random_name():
    """
    Tạo tên tiếng Anh ngẫu nhiên.
    
    Dùng Faker để tạo tên tự nhiên hơn, nếu không có Faker thì dùng danh sách tích hợp.
    
    Trả về:
        str: tên ngẫu nhiên dạng "FirstName LastName"
    """
    if FAKER_AVAILABLE:
        # Dùng Faker tạo trực tiếp tên và họ để tránh prefix/suffix
        # Chọn ngẫu nhiên tên nam hoặc nữ
        if random.choice([True, False]):
            first_name = fake.first_name_male()
        else:
            first_name = fake.first_name_female()
        
        last_name = fake.last_name()
        full_name = f"{first_name} {last_name}"
    else:
        # Quay về danh sách tích hợp
        first_name = random.choice(FIRST_NAMES)
        last_name = random.choice(LAST_NAMES)
        full_name = f"{first_name} {last_name}"
    
    print(f"✅ Đã tạo tên ngẫu nhiên: {full_name}")
    return full_name


def generate_random_birthday():
    """
    Tạo ngày sinh ngẫu nhiên.
    Đảm bảo năm sinh không nhỏ hơn 2000.
    
    Dùng Faker để tạo ngày sinh tự nhiên hơn.
    
    Trả về:
        tuple: (chuỗi năm, chuỗi tháng, chuỗi ngày), ví dụ ("1995", "03", "15")
    """
    from datetime import datetime as dt
    today = dt.now()

    min_birth_year = max(MIN_BIRTH_YEAR, today.year - MAX_AGE)
    max_birth_year = max(min_birth_year, today.year - MIN_AGE)
    birth_year = random.randint(min_birth_year, max_birth_year)
    birth_month = random.randint(1, 12)

    if birth_month in [1, 3, 5, 7, 8, 10, 12]:
        max_day = 31
    elif birth_month in [4, 6, 9, 11]:
        max_day = 30
    else:
        if (birth_year % 4 == 0 and birth_year % 100 != 0) or (birth_year % 400 == 0):
            max_day = 29
        else:
            max_day = 28

    birth_day = random.randint(1, max_day)

    year_str = str(birth_year)
    month_str = str(birth_month).zfill(2)
    day_str = str(birth_day).zfill(2)
    
    print(f"✅ Đã tạo ngày sinh ngẫu nhiên: {year_str}/{month_str}/{day_str}")
    return year_str, month_str, day_str


def generate_user_info():
    """
    Tạo thông tin người dùng ngẫu nhiên đầy đủ.
    
    Trả về:
        dict: chứa tên và ngày sinh
              {
                  'name': 'John Smith',
                  'year': '1995',
                  'month': '03',
                  'day': '15'
              }
    """
    name = generate_random_name()
    year, month, day = generate_random_birthday()
    
    return {
        'name': name,
        'year': year,
        'month': month,
        'day': day
    }


def generate_japan_address():
    """
    Tạo địa chỉ Nhật Bản ngẫu nhiên.
    Dùng Faker để tạo địa chỉ Nhật Bản tự nhiên và đa dạng hơn.
    """
    if FAKER_AVAILABLE:
        # Tạo Faker bản địa hóa Nhật Bản
        fake_jp = Faker('ja_JP')
        
        # Thông tin khu vực của các thành phố chính tại Nhật Bản
        tokyo_wards = [
            {"ward": "Chiyoda-ku", "zip_prefix": "100"},
            {"ward": "Shibuya-ku", "zip_prefix": "150"},
            {"ward": "Shinjuku-ku", "zip_prefix": "160"},
            {"ward": "Minato-ku", "zip_prefix": "105"},
            {"ward": "Meguro-ku", "zip_prefix": "153"},
            {"ward": "Setagaya-ku", "zip_prefix": "154"},
            {"ward": "Nakano-ku", "zip_prefix": "164"},
            {"ward": "Toshima-ku", "zip_prefix": "170"},
        ]
        
        osaka_areas = [
            {"area": "Kita-ku", "zip_prefix": "530"},
            {"area": "Chuo-ku", "zip_prefix": "540"},
            {"area": "Nishi-ku", "zip_prefix": "550"},
            {"area": "Tennoji-ku", "zip_prefix": "543"},
        ]
        
        # Chọn thành phố ngẫu nhiên
        if random.random() < 0.7:  # 70% Tokyo
            ward_info = random.choice(tokyo_wards)
            addr = {
                "zip": f"{ward_info['zip_prefix']}-{random.randint(1000, 9999)}",
                "state": "Tokyo",
                "city": ward_info["ward"],
                "address1": f"{random.randint(1, 9)}-{random.randint(1, 30)}-{random.randint(1, 20)}"
            }
        else:  # 30% Osaka
            area_info = random.choice(osaka_areas)
            addr = {
                "zip": f"{area_info['zip_prefix']}-{random.randint(1000, 9999)}",
                "state": "Osaka",
                "city": area_info["area"],
                "address1": f"{random.randint(1, 9)}-{random.randint(1, 30)}-{random.randint(1, 20)}"
            }
    else:
        # Quay về danh sách địa chỉ cố định cũ
        addresses = [
            {"zip": "100-0005", "state": "Tokyo", "city": "Chiyoda-ku", "address1": "1-1 Marunouchi"},
            {"zip": "160-0022", "state": "Tokyo", "city": "Shinjuku-ku", "address1": "3-14-1 Shinjuku"},
            {"zip": "150-0002", "state": "Tokyo", "city": "Shibuya-ku", "address1": "2-21-1 Shibuya"},
            {"zip": "530-0001", "state": "Osaka", "city": "Osaka-shi", "address1": "1-1 Umeda"},
        ]
        addr = random.choice(addresses)
        random_suffix = f"{random.randint(1, 9)}-{random.randint(1, 20)}"
        addr["address1"] = f"{addr['address1']} {random_suffix}"
    
    print(f"✅ Đã tạo địa chỉ Nhật Bản: {addr['state']} {addr['city']} {addr['address1']}")
    return addr


def generate_us_address():
    """
    Tạo địa chỉ Mỹ ngẫu nhiên.
    Dùng các cụm city/state/zip đã khớp sẵn để tránh lỗi xác định thuế.
    """
    us_localities = [
        {"state": "Delaware", "city": "Wilmington", "zip": "19801"},
        {"state": "Delaware", "city": "Dover", "zip": "19901"},
        {"state": "Delaware", "city": "Newark", "zip": "19702"},
        {"state": "Oregon", "city": "Portland", "zip": "97205"},
        {"state": "Oregon", "city": "Salem", "zip": "97301"},
        {"state": "Oregon", "city": "Eugene", "zip": "97401"},
        {"state": "Montana", "city": "Billings", "zip": "59101"},
        {"state": "Montana", "city": "Missoula", "zip": "59801"},
        {"state": "Montana", "city": "Helena", "zip": "59601"},
        {"state": "New Hampshire", "city": "Manchester", "zip": "03101"},
        {"state": "New Hampshire", "city": "Nashua", "zip": "03060"},
        {"state": "New Hampshire", "city": "Concord", "zip": "03301"},
    ]

    locality = random.choice(us_localities)
    street_number = random.randint(100, 9999)
    street_names = [
        "Main St",
        "Oak Ave",
        "Maple Dr",
        "Cedar Ln",
        "Park Blvd",
        "Washington St",
        "Lincoln Ave",
        "Jefferson Dr",
        "Madison Ln",
    ]
    street = random.choice(street_names)

    if FAKER_AVAILABLE:
        fake_us = Faker('en_US')
        street_suffix = fake_us.secondary_address() if random.random() < 0.18 else ""
        address1 = f"{street_number} {street}"
        if street_suffix:
            address1 = f"{address1} {street_suffix}"
    else:
        address1 = f"{street_number} {street}"

    addr = {
        "zip": locality["zip"],
        "state": locality["state"],
        "city": locality["city"],
        "address1": address1,
    }
    
    print(f"✅ Đã tạo địa chỉ Mỹ: {addr['city']}, {addr['state']} {addr['zip']}")
    return addr


def generate_billing_info(country="JP"):
    """
    Tạo thông tin hóa đơn thanh toán đầy đủ gồm tên và địa chỉ.
    
    Tham số:
        country: mã quốc gia, "JP" hoặc "US"
    
    Trả về:
        dict: thông tin hóa đơn đầy đủ gồm tên và địa chỉ
    """
    # Tạo tên
    name = generate_random_name()
    
    # Tạo địa chỉ theo quốc gia
    if country.upper() == "US":
        address = generate_us_address()
    else:
        address = generate_japan_address()
    
    billing_info = {
        "name": name,
        "zip": address["zip"],
        "state": address["state"],
        "city": address["city"],
        "address1": address["address1"],
        "country": country.upper()
    }
    
    print(f"📋 Đã tạo thông tin hóa đơn đầy đủ:")
    print(f"   Họ tên: {billing_info['name']}")
    print(f"   Địa chỉ: {billing_info['address1']}, {billing_info['city']}")
    print(f"   Bang/tỉnh: {billing_info['state']}, mã bưu chính: {billing_info['zip']}")
    
    return billing_info
