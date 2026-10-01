import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

# Telegram user IDs that can approve invoices.
ADMIN_IDS = [int(item) for item in os.getenv("ADMIN_IDS", "").split(",") if item]
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN")

# Payment settings.
PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "cryptobot")
TELEGRAM_STARS_WEBHOOK_SECRET = os.getenv("TELEGRAM_STARS_WEBHOOK_SECRET", "")

# Stage 9b — способы оплаты на выбор юзера (comma-separated имена
# провайдеров backend'а, например "telegram_stars,lava_top,tribute").
# Пусто или одно имя — меню не показывается, работает старое поведение
# с PAYMENT_PROVIDER выше.
PAYMENT_PROVIDER_CHOICES = [
    p.strip().lower()
    for p in os.getenv("PAYMENT_PROVIDER_CHOICES", "").split(",")
    if p.strip()
]

# Dynamic subscription link base URL (e.g. https://vpn.example.com/sub/)
# Публичные документы сервиса (оферта / возвраты / ПД) — статические страницы
# на домене кабинета (роль deploy_web_frontend кладёт их в camo-root/legal/).
# Пусто → кнопки «О сервисе» не показывают ссылки на документы.
LEGAL_BASE_URL = os.getenv("LEGAL_BASE_URL", "").rstrip("/")

SUB_LINK_BASE_URL = os.getenv("SUB_LINK_BASE_URL", "")

# Запасной фронт саб-ссылки (идёт мимо Cloudflare, прямо на origin) и доля
# токенов на нём, 0..100. Логика ДОЛЖНА совпадать с
# backend/app/services/sub_links.py — бот и бэкенд считают домен независимо,
# и разойтись им нельзя: человек увидит в боте одну ссылку, а кнопка
# «что-то не работает» в клиенте поведёт на другую.
SUB_LINK_BASE_URL_ALT = os.getenv("SUB_LINK_BASE_URL_ALT", "")
SUB_LINK_ALT_SHARE = os.getenv("SUB_LINK_ALT_SHARE", "0")

# Notification polling interval in seconds (0 = disabled)
NOTIFICATION_POLL_INTERVAL = int(os.getenv("NOTIFICATION_POLL_INTERVAL", "10"))

# #62 — Webhook mode. When BOT_WEBHOOK_PORT > 0 the bot starts an
# internal aiohttp server instead of long-polling Telegram. The backend
# receives updates from Telegram (via setWebhook) and forwards
# non-payment ones to http://bot:<port>/webhook.
BOT_WEBHOOK_PORT = int(os.getenv("BOT_WEBHOOK_PORT", "0"))
