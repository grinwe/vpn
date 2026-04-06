import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

# Telegram user IDs that can approve invoices.
ADMIN_IDS = [int(item) for item in os.getenv("ADMIN_IDS", "").split(",") if item]
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN")

# Payment settings. ``PAYMENT_PROVIDER`` controls which driver the
# backend uses when the bot calls /invoices/{id}/checkout. For
# telegram_stars we also need the shared secret that authenticates the
# bot → backend forward of ``successful_payment`` events.
PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "cryptobot")
TELEGRAM_STARS_WEBHOOK_SECRET = os.getenv("TELEGRAM_STARS_WEBHOOK_SECRET", "")
