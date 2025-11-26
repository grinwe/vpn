import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "CHANGE_ME")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
ADMINS = os.getenv("ADMINS", "").split(",") if os.getenv("ADMINS") else []
