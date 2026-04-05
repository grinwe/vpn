import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

# Telegram user IDs that can approve invoices.
ADMIN_IDS = [int(item) for item in os.getenv("ADMIN_IDS", "").split(",") if item]
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN")
