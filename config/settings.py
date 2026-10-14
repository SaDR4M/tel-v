import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
ADMIN_USER_IDS = {
    int(uid.strip())
    for uid in os.getenv("ADMIN_USER_IDS", "").split(",")
    if uid.strip()
}

PANEL_BASE_URL = os.getenv("PANEL_BASE_URL", "").rstrip("/")
PANEL_ADMIN_USERNAME = os.getenv("PANEL_ADMIN_USERNAME", "")
PANEL_ADMIN_PASSWORD = os.getenv("PANEL_ADMIN_PASSWORD", "")

DB_PATH = BASE_DIR / os.getenv("DB_PATH", "data/bot.db")
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "@Sadragjl")
APP_NAME = os.getenv("APP_NAME", "Dante")
CURRENCY = os.getenv("CURRENCY", "IRT")

# Celery / Redis broker used by the reminder worker and beat scheduler.
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")

# Make sure the data directory exists
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Load plans (edit config/plans.json to change pricing/data caps)
with open(BASE_DIR / "config" / "plans.json", "r", encoding="utf-8") as f:
    PLANS = json.load(f)


def get_plan(plan_id: str):
    return next((p for p in PLANS if p["id"] == plan_id), None)
