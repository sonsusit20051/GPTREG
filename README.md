# Công cụ tự động đăng ký tài khoản ChatGPT

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/Python-3.13+-3776AB.svg?logo=python&logoColor=white)
![Code Style](https://img.shields.io/badge/code%20style-black-000000.svg)
![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)
![Maintenance](https://img.shields.io/badge/Maintained%3F-yes-green.svg)

Công cụ tự động hóa toàn bộ quy trình tài khoản ChatGPT bằng Python + Selenium, gồm đăng ký tài khoản, tự động liên kết thẻ để mở Plus và tự động hủy đăng ký.

## Chức Năng

### 1. Tự động đăng ký

- **Quy trình tự động**: tự điền email, mật khẩu, xử lý mã xác minh và điền thông tin cá nhân như ngày sinh.
- **Xử lý mã xác minh**: dùng Hotmail/Outlook có OAuth2 refresh token và API dongvanfb để lấy mã xác minh.
- **Cơ chế chống phát hiện**: dùng `undetected-chromedriver` mô phỏng hành vi người dùng thật để vượt xác minh Cloudflare.
- **Tác vụ hàng loạt**: hỗ trợ cấu hình số lượng đăng ký hàng loạt và khoảng cách giữa các lần chạy.

### 2. Tự động liên kết thẻ

- **Đăng ký Plus**: sau khi đăng ký xong, tự chuyển tới trang đăng ký Plus.
- **Điền thanh toán**: tự điền thông tin thẻ tín dụng, ngày hết hạn, CVC và địa chỉ hóa đơn.
- **Xác nhận trạng thái**: tự kiểm tra trạng thái đăng ký thành công để đảm bảo quyền lợi đã được mở.

### 3. Tự động hủy

- **Giảm rủi ro phát sinh phí**: sau khi đăng ký thành công sẽ hủy đăng ký ngay.
- **Quy trình khép kín**: vào trang cài đặt, quản lý đăng ký, hủy gói và xác nhận hủy.
- **Giữ quyền lợi chu kỳ hiện tại**: sau khi hủy, tài khoản vẫn giữ quyền Plus trong chu kỳ thanh toán hiện tại.

## Cấu Trúc Dự Án

```text
py/
├── main.py              # Logic CLI chính
├── server.py            # Web server Flask
├── browser.py           # Module tự động hóa trình duyệt Selenium + undetected-chromedriver
├── email_service.py     # Service Hotmail/Outlook lấy mã qua API dongvanfb
├── config.py            # Module tải cấu hình
├── utils.py             # Hàm tiện ích dùng chung
├── static/              # Tài nguyên frontend HTML/CSS/JS
├── config.yaml          # File cấu hình thực tế, chứa thông tin riêng tư
├── config.example.yaml  # File cấu hình mẫu
├── pyproject.toml       # Định nghĩa dependency bằng uv
├── uv.lock              # File khóa dependency
└── README.md            # Tài liệu hướng dẫn
```

## Chạy Nhanh

### 1. Cài dependency

Dự án dùng [uv](https://github.com/astral-sh/uv) để quản lý package.

```bash
pip install uv
uv sync
```

### 2. Cấu hình

Sao chép file cấu hình mẫu rồi chỉnh thông tin trong `config.yaml`:

```bash
cp config.example.yaml config.yaml
```

### 3. Chạy

Web console:

```bash
uv run server.py
```

Sau đó mở: [http://localhost:5050](http://localhost:5050)

Telegram bot:

```bash
TELEGRAM_BOT_TOKEN="BOT_TOKEN" .venv/bin/python3 telegram_bot.py
```

Hoặc để tránh token xuất hiện trong process command:

```bash
TELEGRAM_BOT_TOKEN_FILE="/đường/dẫn/token.txt" .venv/bin/python3 telegram_bot.py
```

Lệnh bot:

```text
/menu
/regget mail|pass|refresh_token|client_id
mail2|pass2|refresh_token2|client_id2
/pass mật_khẩu_mặc_định
/ban username_or_id
```

Nếu chưa cấu hình `TELEGRAM_ADMIN_IDS`, user đầu tiên gửi `/start` sẽ được ghi làm admin trong `telegram_bot_state.json`. User thường chỉ được chạy 1 job tại một thời điểm và tối đa 2 cụm mail mỗi lệnh; admin có thể chạy song song tối đa 2 cụm.

CLI:

```bash
uv run main.py
```

## Cấu Hình

Tất cả cấu hình nằm trong `config.yaml`, dùng định dạng YAML.

Các mục bắt buộc:

| Mục cấu hình | Đường dẫn | Mô tả |
| --- | --- | --- |
| File Hotmail | `email.accounts_file` | File chứa danh sách Hotmail theo format `email|password|refresh_token|client_id` |
| API lấy mã | `email.api_url` | Endpoint lấy mã xác minh OAuth2 |
| Loại mail | `email.type` | Đặt là `facebook` để lấy đúng OTP OpenAI/ChatGPT qua dongvanfb |

Các mục tùy chọn:

| Mục cấu hình | Đường dẫn | Mặc định | Mô tả |
| --- | --- | --- | --- |
| Số lượng đăng ký | `registration.total_accounts` | 1 | Số tài khoản cần đăng ký |
| Tuổi nhỏ nhất | `registration.min_age` | 20 | Tuổi nhỏ nhất để tạo ngày sinh ngẫu nhiên |
| Tuổi lớn nhất | `registration.max_age` | 40 | Tuổi lớn nhất để tạo ngày sinh ngẫu nhiên |
| Độ dài mật khẩu | `password.length` | 16 | Độ dài mật khẩu |
| Timeout email | `email.wait_timeout` | 120 | Thời gian chờ email xác minh, tính bằng giây |
| Poll interval | `email.poll_interval` | 5 | Khoảng cách gọi API lấy mã, tính bằng giây |

## Module

### `config.py`

Tải cấu hình từ `config.yaml`, hỗ trợ tự tìm file `.yaml`, `.yml`, `.local.yaml`, truy cập kiểu dataclass, tương thích import hằng số cũ và tải lại cấu hình khi chạy.

### `email_service.py`

Dùng file `hotmail_accounts.txt` để lấy lần lượt từng tài khoản Hotmail theo vòng round-robin, sau đó gọi API `https://tools.dongvanfb.net/api/get_code_oauth2` để lấy mã xác minh.

### `browser.py`

Dùng `undetected-chromedriver` để tự động hóa trình duyệt: tạo driver, điền form đăng ký, nhập mã xác minh, điền hồ sơ, mở dùng thử Plus và hủy đăng ký.

### `utils.py`

Chứa các hàm phụ trợ: tạo HTTP session có retry, tạo mật khẩu ngẫu nhiên, lưu tài khoản vào TXT, cập nhật trạng thái tài khoản và trích xuất mã xác minh.

### `main.py`

Điểm vào chương trình, kết hợp các module để đăng ký một tài khoản hoặc đăng ký hàng loạt.

## Lưu Ý Bảo Mật

1. Không commit `config.yaml` vì file này chứa thông tin nhạy cảm như API key hoặc thông tin thẻ.
2. Dự án đã cấu hình `.gitignore` để bỏ qua `config.yaml`.
3. Dùng `config.example.yaml` làm mẫu cấu hình.
4. Kiểm tra định kỳ và xóa bản ghi tài khoản đã lưu nếu không còn cần.

## Lưu Ý Vận Hành

1. Cần điền đúng `refresh_token` và `client_id` trong `hotmail_accounts.txt`.
2. Đảm bảo tài khoản Hotmail/Outlook còn hoạt động và API dongvanfb trả được mã.
3. Không thao tác cửa sổ trình duyệt trong quá trình đăng ký.
4. Nên đặt khoảng cách giữa các lần đăng ký để giảm rủi ro bị kiểm soát.

## File Đầu Ra

Tài khoản đăng ký thành công được lưu vào `registered_accounts.txt` theo định dạng:

```text
email | mật khẩu | trạng thái | thời gian đăng ký
xxx@domain.com | password123 | Đã hủy đăng ký | 2026-01-06 09:45:00
```

Dependency của dự án hiện được quản lý bằng `pyproject.toml`.

## Miễn Trừ Trách Nhiệm

1. **Mục đích nghiên cứu kỹ thuật**: dự án chỉ dùng cho học tập và nghiên cứu tự động hóa Python, nhằm kiểm chứng tính khả thi của Selenium và undetected-chromedriver.
2. **Sử dụng hợp lệ**: vui lòng tuân thủ [Điều khoản sử dụng của OpenAI](https://openai.com/policies/terms-of-use). Không dùng công cụ cho mục đích thương mại, đăng ký hàng loạt quy mô lớn hoặc hành vi vi phạm điều khoản dịch vụ.
3. **Tự chịu rủi ro**: người dùng tự chịu mọi hậu quả phát sinh, bao gồm nhưng không giới hạn ở khóa tài khoản hoặc chặn IP.
4. **Không bảo đảm**: dự án được chia sẻ theo tinh thần mã nguồn mở, không cam kết bảo hành hoặc bảo trì. Code có thể mất hiệu lực khi website mục tiêu thay đổi.
