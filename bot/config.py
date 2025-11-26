import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "CHANGE_ME")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

# Telegram user IDs that can approve invoices. Replace with real admin IDs.
ADMIN_IDS = [123456789]
