import re
from aiogram.exceptions import TelegramBadRequest


def sanitize_username(raw: str) -> str:
    """Lowercase, strip anything that isn't a-z0-9_, collapse repeats,
    and cap length so it's safe to use as a panel username."""
    cleaned = raw.strip().lower()
    cleaned = re.sub(r"[^a-z0-9_]", "", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "user"
    return cleaned[:20]


def make_panel_username(raw: str, telegram_id: int) -> str:
    """Combines a sanitized user-chosen name with their Telegram ID so it's
    guaranteed unique on the panel even if two people pick the same name."""
    return f"{sanitize_username(raw)}_{telegram_id}"


async def safe_edit_text(message, text, reply_markup=None):
    """Edits a message's text, silently ignoring Telegram's harmless
    'message is not modified' error (fires when a button is tapped twice
    in a row and the resulting text/markup would be identical)."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise