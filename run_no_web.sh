#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

TOKEN_FILE="${TELEGRAM_BOT_TOKEN_FILE:-$PWD/telegram_token.txt}"
ADMIN_IDS="${TELEGRAM_ADMIN_IDS:-5047039998}"
RUNNING_BOT_PIDS="$(pgrep -f '[t]elegram_bot.py' || true)"

if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "Không tìm thấy token file: $TOKEN_FILE"
  echo "Tạo file telegram_token.txt hoặc truyền TELEGRAM_BOT_TOKEN_FILE=/đường/dẫn/token.txt"
  exit 1
fi

if [[ -n "$RUNNING_BOT_PIDS" && "${FORCE_RUN_NO_WEB:-0}" != "1" ]]; then
  echo "Đang có Telegram bot khác chạy, không khởi động thêm để tránh lỗi 409 Conflict:"
  echo "$RUNNING_BOT_PIDS"
  echo "Dừng bot cũ trước, hoặc chạy FORCE_RUN_NO_WEB=1 ./run_no_web.sh nếu bạn chắc chắn."
  exit 1
fi

echo "Chạy Telegram bot, không chạy web backend..."
echo "Token file: $TOKEN_FILE"
echo "Admin IDs: $ADMIN_IDS"
echo "Dừng bằng Ctrl+C"

TELEGRAM_BOT_TOKEN_FILE="$TOKEN_FILE" TELEGRAM_ADMIN_IDS="$ADMIN_IDS" .venv/bin/python3 telegram_bot.py
