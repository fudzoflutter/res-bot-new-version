"""
Text formatting helpers (HTML parse mode).

All user-facing messages are HTML.  Centralising escaping/mention helpers here
keeps handler code short and prevents markup injection from user content.

FAQAT ishlatiladigan yordamchilar qolgan (dead code olib tashlangan):
``esc`` (HTML escape), ``mention_by_id`` (havola), ``fmt_number`` (raxamlar).
"""

from __future__ import annotations

import html
from typing import Optional


def esc(value: Optional[str]) -> str:
    """Escape a value for safe inclusion in HTML text."""
    return html.escape(str(value)) if value is not None else ""


def mention_by_id(user_id: int, name: str, username: Optional[str] = None) -> str:
    """Mention built from stored DB values (user may be long gone)."""
    label = name or (f"@{username}" if username else str(user_id))
    if username:
        return f'<a href="https://t.me/{username}">{esc(label)}</a>'
    return f'<a href="tg://user?id={user_id}">{esc(label)}</a>'


def fmt_number(value: int) -> str:
    """1234567 -> '1 234 567' (space as thousands separator)."""
    return f"{value:,}".replace(",", " ")
