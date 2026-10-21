from collections import Counter

import re
from aiogram.exceptions import TelegramBadRequest

# Card number without separator spaces: inside Persian (RTL) messages,
# space-separated digit groups get laid out right-to-left by the bidi
# algorithm and visually reverse — one continuous run keeps its order.
# The LTR mark guards any client that re-resolves the run's direction.
CARD_NUMBER = "\u200e6219861086485009"


def payment_card_line() -> str:
    """Card number + holder, direction-safe for RTL messages."""
    return (
        f"کارت: <code>{CARD_NUMBER}</code>\n"
        "به نام: <b>صدرا آقاجانلو</b>"
    )


def dedupe_labels(labels: list[str]) -> list[str]:
    """Tag labels that would render identically (e.g. two subscriptions with
    the same nickname and plan: «Sadra» — 10 گیگ showed twice) with #1, #2, ...
    Uniqueness there comes from the list position, so it's deterministic:
    #1 is the most recent order. Display-only; nothing is renamed."""
    total = Counter(labels)
    seen: Counter = Counter()
    out = []
    for label in labels:
        if total[label] > 1:
            seen[label] += 1
            out.append(f"{label} #{seen[label]}")
        else:
            out.append(label)
    return out


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


async def safe_edit_text(message, text, reply_markup=None, parse_mode=None):
    """Edits a message's text, silently ignoring Telegram's harmless
    'message is not modified' error (fires when a button is tapped twice
    in a row and the resulting text/markup would be identical)."""
    kwargs = {"reply_markup": reply_markup}
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


async def safe_edit_caption(message, caption, reply_markup=None, parse_mode=None):
    """Caption counterpart of safe_edit_text — same double-tap race on order
    review messages (approve/reject/edit by two admins or a retried tap)."""
    kwargs = {"reply_markup": reply_markup}
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    try:
        await message.edit_caption(caption=caption, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise