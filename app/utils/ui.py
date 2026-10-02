"""
Inline keyboard factory helpers.

* ``BtnStyle`` – colored buttons (Bot API 9.4+).
* ``btn``      – one button with optional color / premium emoji / copy-text.
* ``kb``       – turn rows of buttons into an InlineKeyboardMarkup.

NOTE on premium emojis: a bot can display ``icon_custom_emoji_id`` on its
buttons/messages only if the *bot account itself* has Telegram Premium or
owns a Fragment username; otherwise Telegram silently shows plain text.
Keep IDs even then — the day you add Premium to the bot account they
start rendering automatically.
"""

from __future__ import annotations

from typing import Optional, Sequence

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup


# ---------------------------------------------------------------------------
# Button styles (Bot API 9.4+): 'danger' = red, 'success' = green,
# 'primary' = blue.  Plain buttons (style omitted) use the app default.
# ---------------------------------------------------------------------------
class BtnStyle:
    DANGER = "danger"
    SUCCESS = "success"
    PRIMARY = "primary"


def btn(
    text: str,
    callback_data: Optional[str] = None,
    *,
    url: Optional[str] = None,
    style: Optional[str] = None,
    emoji_id: Optional[str] = None,
    copy_text: Optional[str] = None,
) -> InlineKeyboardButton:
    """Build one inline button with optional color / premium emoji / copy.

    Exactly one *action* is required: callback_data, url or copy_text.
    """
    payload: dict = {"text": text}
    if callback_data:
        payload["callback_data"] = callback_data
    if url:
        payload["url"] = url
    if copy_text:
        payload["copy_text"] = CopyTextButton(text=copy_text)
    if style:
        payload["style"] = style
    if emoji_id:
        payload["icon_custom_emoji_id"] = emoji_id
    return InlineKeyboardButton(**payload)


def kb(rows: Sequence[Sequence[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    """Build an InlineKeyboardMarkup from rows of buttons."""
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows])
