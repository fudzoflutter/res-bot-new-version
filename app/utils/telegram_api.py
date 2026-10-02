"""
Telegram API chaqiruvlari uchun umumiy himoya qatlami.

MUAMMO: Telegram API vaqtinchalik xatolar beradi (429 RetryAfter, tarmoq
uzilishi, server xatosi).  Har bir joyda alohida try/except yozilsa, ba'zi
chaqiruvlar cheksiz/qisqa urinishlar bilan qolib ketadi va hisobotlar
yo'qoladi.

YECHIM — BITTA joyda:

* ``429 (RetryAfter)``    — Telegram aytgan vaqt KUTILADI (``retry_after``),
  keyin qayta uriniladi; urinishlar soni cheklangan (cheksiz emas).
* ``timeout / network``   — eksponensial backoff (1s -> 2s -> 4s…).
* ``409 Conflict``        — qayta urinilmaydi (bu boshqa nusxa polling
  qilayotgani — :mod:`app.services.duplicate_watch` alohida xabar beradi).
* ``BadRequest / Forbidden`` — qayta urinilmaydi (xato doimiy).

Shu bilan birga:

* umumiy CONCURRENCY limit (bir vaqtda juda ko'p so'rov ketmasin);
* har bir chaqiruv uchun TIMEOUT (osilib qolmaslik);
* statistika — admin panel "🤖 Bot Health" shu yerdan o'qiydi.

Modul hech qachon xatoni yutmaydi: urinishlar tugagach xato yuqoriga
ko'tariladi (chaqiruvchi o'zi hal qiladi).
"""

from __future__ import annotations

from app.utils.timeutils import local_now

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from aiogram.exceptions import (
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

logger = logging.getLogger(__name__)

# Bir vaqtda ketadigan Telegram so'rovlari (flood oldini oladi).
TG_CONCURRENCY = 4

# Standart urinishlar va backoff.
DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 1.0
DEFAULT_MAX_DELAY = 30.0

# Chiqish so'rovlari uchun umumiy limit.
_semaphore: Optional[asyncio.Semaphore] = None


def _limit() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(TG_CONCURRENCY)
    return _semaphore


class TelegramApiStats:
    """Telegram API statistikasi (health ekrani uchun, xotira ichida)."""

    def __init__(self) -> None:
        self.calls = 0
        self.failures = 0
        self.retries = 0
        self.retry_after_count = 0
        self.conflict_count = 0
        self.network_errors = 0
        self.timeouts = 0
        self.last_error: str = ""
        self.last_call_at: Optional[datetime] = None
        self.last_retry_after: int = 0
        self.consecutive_retry_after = 0

    def record_call(self) -> None:
        self.calls += 1
        self.last_call_at = local_now()

    def record_success(self) -> None:
        self.consecutive_retry_after = 0

    def record_error(self, exc: BaseException) -> None:
        self.failures += 1
        self.last_error = f"{type(exc).__name__}: {exc}"[:200]

    @property
    def status(self) -> str:
        """OK / WARN (429 ko'p) / ERROR (oxirgi so'rov xato bilan tugagan)."""
        if self.last_error and self.consecutive_retry_after > 2:
            return "WARN"
        return "OK"

    def snapshot(self) -> dict:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "retries": self.retries,
            "retry_after": self.retry_after_count,
            "conflicts": self.conflict_count,
            "network_errors": self.network_errors,
            "timeouts": self.timeouts,
            "last_error": self.last_error or "—",
            "last_call_at": self.last_call_at.strftime("%d.%m.%Y %H:%M:%S") if self.last_call_at else "—",
            "status": self.status,
        }


stats = TelegramApiStats()


async def call(
    op: str,
    factory: Callable[[], Awaitable[Any]],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    timeout: Optional[float] = 60.0,
    limited: bool = True,
) -> Any:
    """Telegram API chaqiruvini himoyalangan holda bajaradi.

    ``factory`` — parametrsiz korutina funksiyasi, masalan::

        await call("send", lambda: bot.send_message(chat, text))

    Chaqiruvchi xato ko'tarilishiga tayyor bo'lishi kerak (urinishlar
    tugagach xato o'zgarishsiz yuqoriga chiqadi).
    """
    attempts = max(1, int(attempts))
    last: Optional[BaseException] = None

    async def _invoke() -> Any:
        if timeout:
            return await asyncio.wait_for(factory(), timeout=timeout)
        return await factory()

    for attempt in range(1, attempts + 1):
        stats.record_call()
        try:
            if limited:
                async with _limit():
                    result = await _invoke()
            else:
                result = await _invoke()
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter as exc:
            stats.retry_after_count += 1
            stats.consecutive_retry_after += 1
            stats.last_retry_after = int(exc.retry_after)
            stats.record_error(exc)
            _warn_flood()
            if attempt >= attempts:
                last = exc
                break
            wait = max(1.0, float(exc.retry_after)) + 0.5
            logger.warning(
                "Telegram 429 (%s): %.1fs kutish (urinish %d/%d)",
                op, wait, attempt, attempts,
            )
            await asyncio.sleep(wait)
        except TelegramConflictError as exc:
            stats.conflict_count += 1
            stats.record_error(exc)
            # 409 — boshqa nusxa polling qilmoqda; qayta urinish foydasiz.
            raise
        except TelegramForbiddenError as exc:
            stats.record_error(exc)
            raise
        except (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError) as exc:
            stats.record_error(exc)
            if isinstance(exc, asyncio.TimeoutError):
                stats.timeouts += 1
            else:
                stats.network_errors += 1
            if attempt >= attempts:
                last = exc
                break
            stats.retries += 1
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            logger.warning(
                "Telegram vaqtincha xatosi (%s): %.1fs dan keyin qayta urinish %d/%d — %s",
                op, delay, attempt + 1, attempts, exc,
            )
            await asyncio.sleep(delay)
        except Exception as exc:  # noqa: BLE001 – doimiy xato (BadRequest va h.k.)
            stats.record_error(exc)
            raise
        else:
            stats.record_success()
            return result

    if last is not None:
        stats.record_error(last)
        raise last
    raise RuntimeError(f"Telegram API chaqiruvi bajarilmadi: {op}")  # pragma: no cover


def _warn_flood() -> None:
    """Ketma-ket 3+ marta 429 bo'lsa OWNERlarga bir marta ogohlantiradi."""
    if stats.consecutive_retry_after < 3:
        return
    try:
        from app.services.alerts import SEVERITY_WARNING, alerts

        async def _send() -> None:
            await alerts.notify(
                "telegram_flood",
                "TELEGRAM API: 429 RETRYAFTER",
                "Ketma-ket bir nechta so'rov 429 bilan qaytdi.\n"
                f"Oxirgi RetryAfter: {stats.last_retry_after}s\n"
                f"Jami 429: {stats.retry_after_count}\n"
                "Tekshiring: bot bir nechta joyda ishlamayaptimi (409 bo'lmasa ham).",
                severity=SEVERITY_WARNING,
            )

        asyncio.get_running_loop().create_task(_send())
    except RuntimeError:
        pass  # event loop yo'q (testlar) — jim o'tadi
