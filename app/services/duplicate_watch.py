"""
409 CONFLICT kuzatuvchisi — "botni ikki nusxa poll qilmoqda" ni JIMGINA
qoldirmaydi.

MUAMMO
------
aiogram ichidagi polling tsikli 409 (``Conflict: terminated by other
getUpdates request``) xatosini O'ZI yutib, faqat backoff bilan qayta
urinadi.  Shu sababli tashqi dunyo (va SIZ) buni ko'rmaydi: bot ishlayapti
ko'rinadi, lekin update'larning bir qismini BOSHQA nusxa olib ketyapti —
"ba'zi hisobot kelmayapti" degan shikoyat aynan shundan chiqadi.

YECHIM
------
``aiogram.dispatcher`` loglariga quloq soladigan handler: 60 sekund ichida
3+ marta 409 bo'lsa, adminga Telegramga ANIQ ko'rsatma bilan xabar
yuboriladi (keyingilari 30 daqiqadan keyin) va logga ERROR yoziladi.

Bu qatlam ataylab LOG darajasida ishlaydi: polling kodini o'zgartirmaydi,
shuning uchun aiogram yangilanishlarida ham ishlashda davom etadi.
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable, Optional

from app.utils.tasks import spawn

# Faqat shu logger'da va faqat shu matn bilan kelgan yozuvlar sanaladi.
WATCHED_LOGGER = "aiogram.dispatcher"
CONFLICT_MARKER = "Conflict: terminated by other getUpdates request"

# 60 sekund ichida 3 ta 409 — "musobaqa" bor deb hisoblaymiz.
WINDOW_SECONDS = 60.0
TRIGGER_COUNT = 3

# Ogohlantirishlar orasidagi minimal vaqt (spam bo'lmasligi uchun):
# birinchi ogohlantirish darhol keladi, keyingilari 30 daqiqadan keyin.
ALERT_COOLDOWN_SECONDS = 30 * 60.0

_watcher: Optional["ConflictWatcher"] = None


class ConflictWatcher(logging.Handler):
    """409 xatolarini sanaydi va chegaradan oshsa bir marta xabar beradi."""

    def __init__(
        self,
        alert: Callable[[int], Awaitable[None]],
        *,
        window_seconds: float = WINDOW_SECONDS,
        trigger_count: int = TRIGGER_COUNT,
        cooldown_seconds: float = ALERT_COOLDOWN_SECONDS,
    ) -> None:
        super().__init__(level=logging.ERROR)
        self._alert = alert
        self._window = window_seconds
        self._trigger = trigger_count
        self._cooldown = cooldown_seconds
        self._times: list[float] = []
        self._last_alert: float = 0.0
        self.total = 0          # jarayon boshidan jami 409 soni
        self.alerts = 0         # yuborilgan ogohlantirishlar soni

    # -- logging.Handler API -------------------------------------------------
    def emit(self, record: logging.LogRecord) -> None:
        """Har bir ERROR yozuvi uchun chaqiriladi (hech qachon xato bermasin)."""
        try:
            if record.name != WATCHED_LOGGER:
                return
            if CONFLICT_MARKER not in self._message(record):
                return
            self._on_conflict()
        except Exception:  # noqa: BLE001 – logging xato ko'tarmasligi shart
            pass

    # -- ichki mantiq --------------------------------------------------------
    def _on_conflict(self) -> None:
        now = time.monotonic()
        self.total += 1
        self._times = [t for t in self._times if now - t < self._window]
        self._times.append(now)
        if len(self._times) < self._trigger:
            return
        # DIQQAT: `_last_alert` 0.0 = "hali ogohlantirilmagan".  Buni "juda
        # eski vaqt" deb hisoblab bo'lmaydi: `time.monotonic()` yangi
        # yuklangan mashina/konteynerda kichik bo'ladi (uptime < cooldown),
        # natijada BIRINCHI ogohlantirish — eng kerakli paytda — yutilib
        # ketardi.  "Hali ogohlantirilmagan" holatini alohida tekshiramiz.
        if self._last_alert and now - self._last_alert < self._cooldown:
            return
        self._last_alert = now
        self.alerts += 1
        # Yuborish FONDA: logging chaqiruvini hech narsa kutdirmasin.
        spawn(self._alert(self.total), name="conflict-alert")

    @staticmethod
    def _message(record: logging.LogRecord) -> str:
        try:
            return record.getMessage()
        except Exception:  # noqa: BLE001 – formatlashda xato bo'lsa
            return ""


def install(alert: Callable[[int], Awaitable[None]]) -> ConflictWatcher:
    """Kuzatuvchini ``aiogram.dispatcher`` logger'iga o'rnatadi.

    Ikki marta chaqirilsa eski handler olib tashlanadi (restart/testlar).
    """
    global _watcher
    if _watcher is not None:
        uninstall()
    _watcher = ConflictWatcher(alert)
    logging.getLogger(WATCHED_LOGGER).addHandler(_watcher)
    return _watcher


def uninstall() -> None:
    """Kuzatuvchini olib tashlaydi."""
    global _watcher
    if _watcher is None:
        return
    logging.getLogger(WATCHED_LOGGER).removeHandler(_watcher)
    _watcher = None


def current() -> Optional[ConflictWatcher]:
    """O'rnatilgan kuzatuvchi (testlar/diagnostika uchun)."""
    return _watcher
