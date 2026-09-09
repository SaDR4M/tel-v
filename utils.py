import re
from random import randint


def sanitize_username(raw: str) -> str:
    """Lowercase, strip anything that isn't a-z0-9_, collapse repeats,
    and cap length so it's safe to use as a panel username."""
    cleaned = raw.strip().lower()
    cleaned = re.sub(r"[^a-z0-9_]", "", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "user"
    return cleaned[:20]

def random_number():
    return randint(100000,9999999)

def make_panel_username(raw: str, telegram_id: int) -> str:
    """Combines a sanitized user-chosen name with random ID so it's
    guaranteed unique on the panel even if two people pick the same name."""
    print(telegram_id)
    print(type(telegram_id))
    return f"{sanitize_username(raw)}_{str(telegram_id)[:5] + str(random_number())}"
