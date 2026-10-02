"""
Yengil ish hisoblagichlari (update / xato / ogohlantirish soni, uptime).

Oldin bular chat-admin panel ichida edi; endi alohida modulda — chat panel
olib tashlangan, hisoblagichlar esa :class:`app.main.MetricsMiddleware`
tomonidan yuritiladi.
"""

from __future__ import annotations

import time
from typing import Optional

_bot_start_time: float = time.monotonic()
_update_count: int = 0
_error_count: int = 0
_warning_count: int = 0
_last_update_time: Optional[float] = None


def record_update() -> None:
    global _update_count, _last_update_time
    _update_count += 1
    _last_update_time = time.monotonic()


def record_error() -> None:
    global _error_count
    _error_count += 1


def record_warning() -> None:
    global _warning_count
    _warning_count += 1


def snapshot() -> dict:
    """Joriy hisoblagichlar (diagnostika/testlar uchun)."""
    return {
        "updates": _update_count,
        "errors": _error_count,
        "warnings": _warning_count,
        "uptime_seconds": int(time.monotonic() - _bot_start_time),
    }
