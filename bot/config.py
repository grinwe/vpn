import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

# Telegram user IDs that can approve invoices.
ADMIN_IDS = [int(item) for item in os.getenv("ADMIN_IDS", "").split(",") if item]
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN")

# Payment settings.
PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "cryptobot")
TELEGRAM_STARS_WEBHOOK_SECRET = os.getenv("TELEGRAM_STARS_WEBHOOK_SECRET", "")

# Dynamic subscription link base URL (e.g. https://vpn.example.com/sub/)
SUB_LINK_BASE_URL = os.getenv("SUB_LINK_BASE_URL", "")

# Notification polling interval in seconds (0 = disabled)
NOTIFICATION_POLL_INTERVAL = int(os.getenv("NOTIFICATION_POLL_INTERVAL", "10"))
