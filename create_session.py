from telethon.sync import TelegramClient
import os
from dotenv import load_dotenv

load_dotenv()

API_ID   = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")

if not API_ID or not API_HASH:
    raise RuntimeError("Укажите TG_API_ID и TG_API_HASH в .env")

# Этот же SESSION_FILE, что и в основном боте
SESSION_FILE = "session_fetcher.session"

with TelegramClient(SESSION_FILE, API_ID, API_HASH) as client:
    print("✅ Успешно авторизованы, сессия сохранена в", SESSION_FILE)
