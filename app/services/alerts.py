"""
Admin alert xizmati (cooldown bilan).

MUAMMO: kritik xatolar (baza uzildi, ulanish uzildi, watchdog qayta ishga
tushdi) admin panelni ochmaguncha ko'rinmaydi.  YECHIM: shu modul ularni
TO'G'RIDAN-TO'G'RI adminga Telegramga yuboradi va faqat bir marta — keyingi
bir xil xabar cooldown tugaguncha yuborilmaydi (spam bo'lmasligi uchun).

QATLAM QOIDALARI:
* hech qachon istisno ko'tarmaydi — alert yuborilmasa ham bot ishlaydi;
* matn HTML-escape qilinadi (foydalanuvchi/DB matni panelni buzmasligi uchun);
* yuborish FONDA emas, `await` bilan — chaqiruvchi muvaffaqiyatsizlikni
  bilishi kerak (lekin xato ko'tarilmaydi).

Bir xil xato takrorlansa — ``key`` (masalan ``db_error``) bo'yicha
throttling ishlaydi.  Har bir yuborilgan alert ``activity_log``ga ham
yoziladi (admin panelda tarix qoladi).
"""

from __future__ import annotations

from app.emoji_config import EMOJI

from app.utils.timeutils import local_now

import logging
import time
from collections import deque
from datetime import datetime
from typing import Awaitable, Callable, Optional

from app.config import settings
from app.utils.formatting import esc

logger = logging.getLogger(__name__)

# Severity — activity_log bilan bir xil qiymatlar.
SEVERITY_INFO = "INFO"
SEVERITY_WARNING = "WARNING"
SEVERITY_ERROR = "ERROR"
SEVERITY_CRITICAL = "CRITICAL"

# Bir xil kalit uchun minimal qayta yuborish oralig'i (sekund).
DEFAULT_COOLDOWN = 15 * 60.0
COOLDOWNS = {
    SEVERITY_INFO: 60 * 60.0,
    SEVERITY_WARNING: 30 * 60.0,
    SEVERITY_ERROR: 15 * 60.0,
    SEVERITY_CRITICAL: 5 * 60.0,
}

ICONS = {
    SEVERITY_INFO: "ℹ️",
    SEVERITY_WARNING: EMOJI.warning.plain,
    SEVERITY_ERROR: EMOJI.offline_dot.plain,
    SEVERITY_CRITICAL: EMOJI.critical.plain,
}

Sender = Callable[[str], Awaitable[None]]


class AlertService:
    """Admin alertlarini yig'adi, throttling qiladi va yuboradi."""

    def __init__(self, history_size: int = 50) -> None:
        self._sender: Optional[Sender] = None
        self._last_sent: dict[str, float] = {}
        self._counts: dict[str, int] = {}
        self._suppressed: dict[str, int] = {}
        self.history: deque[dict] = deque(maxlen=history_size)
        self.sent_total = 0
        self.suppressed_total = 0

    # -- sozlash ------------------------------------------------------------
    def configure(self, sender: Optional[Sender]) -> None:
        """Sender — matnni adminga yuboradigan korutina (odatda ``bot.send_message``)."""
        self._sender = sender

    @property
    def configured(self) -> bool:
        return self._sender is not None

    # -- throttling ---------------------------------------------------------
    def is_cooling(self, key: str, severity: str = SEVERITY_ERROR) -> bool:
        last = self._last_sent.get(key)
        if last is None:
            return False
        cooldown = COOLDOWNS.get(severity, DEFAULT_COOLDOWN)
        return (time.monotonic() - last) < cooldown

    # -- yuborish -----------------------------------------------------------
    async def notify(
        self,
        key: str,
        title: str,
        body: str = "",
        *,
        severity: str = SEVERITY_ERROR,
        cooldown: Optional[float] = None,
        force: bool = False,
        also_log: bool = True,
        connection_id: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> bool:
        """Adminga alert yuboradi.

        ``key`` — throttling kaliti (masalan ``"db_error"``).  Bir xil kalit
        cooldown ichida takrorlansa — yuborilmaydi (``False`` qaytadi) va
        faqat WARNING log yoziladi.

        Qaytaradi: ``True`` — yuborildi, ``False`` — throttled/sozlanmagan.
        """
        now = time.monotonic()
        limit = COOLDOWNS.get(severity, DEFAULT_COOLDOWN) if cooldown is None else float(cooldown)
        last = self._last_sent.get(key)
        if not force and last is not None and (now - last) < limit:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            self.suppressed_total += 1
            logger.warning("Alert suppressed (cooldown %ss): %s", int(limit), key)
            return False

        icon = ICONS.get(severity, EMOJI.warning.plain)
        safe_title = esc(title)
        safe_body = esc(body) if body else ""
        text = f"{icon} <b>{safe_title}</b>"
        if safe_body:
            text += f"\n\n{safe_body}"

        delivered = False
        if self._sender is not None:
            try:
                await self._sender(text)
                delivered = True
            except Exception:  # noqa: BLE001 – alert yuborilmasa bot to'xtamaydi
                logger.exception("Alert yuborilmadi: %s", key)
        else:
            logger.warning("Alert (sender sozlanmagan): %s — %s", key, title)

        if not delivered:
            # Yuborilmadi — cooldownni yozmaymiz, keyingi urinishda qayta
            # yuborilishi mumkin (lekin log yozuv qoladi).
            return False

        self._last_sent[key] = now
        self._counts[key] = self._counts.get(key, 0) + 1
        self.sent_total += 1
        self.history.append(
            {
                "key": key,
                "severity": severity,
                "title": title,
                "at": local_now().strftime("%d.%m.%Y %H:%M:%S"),
            }
        )
        if also_log:
            await self.log_activity(
                key, title, severity=severity,
                connection_id=connection_id, user_id=user_id,
            )
        return True

    async def log_activity(
        self,
        event_type: str,
        description: str,
        *,
        severity: str = SEVERITY_ERROR,
        connection_id: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> None:
        """Alertni activity_log jadvaliga ham yozadi (panelda ko'rinadi)."""
        try:
            from app.database import db

            await db.add_activity_log(
                event_type,
                description,
                connection_id=connection_id,
                user_id=user_id,
                severity=severity,
            )
        except Exception:  # noqa: BLE001 – DB o'lgan bo'lsa ham davom
            logger.debug("Activity log yozilmadi (DB xatosi?)", exc_info=True)

    # -- diagnostika --------------------------------------------------------
    def status(self) -> dict:
        """Admin panel uchun qisqa holat."""
        active = {
            key: int(COOLDOWNS.get(SEVERITY_ERROR, DEFAULT_COOLDOWN) - (time.monotonic() - at))
            for key, at in self._last_sent.items()
            if (time.monotonic() - at) < COOLDOWNS.get(SEVERITY_ERROR, DEFAULT_COOLDOWN)
        }
        return {
            "configured": self.configured,
            "sent_total": self.sent_total,
            "suppressed_total": self.suppressed_total,
            "cooling_keys": active,
            "counts": dict(self._counts),
        }

    def reset(self) -> None:
        """Testlar uchun holatni tozalash."""
        self._last_sent.clear()
        self._counts.clear()
        self._suppressed.clear()
        self.history.clear()
        self.sent_total = 0
        self.suppressed_total = 0


# Global xizmat (butun protsess uchun bitta).
alerts = AlertService()


def configure_default_sender(bot) -> None:  # type: ignore[type-arg]
    """Botga barcha OWNERlarga yuboradigan sender o'rnatadi.

    Ko'p adminli arxitektura: alertlar faqat OWNERlarga boradi (adminlar
    monitoringni panelda ko'radi; kritik xabarlar egasiga boradi).
    """
    targets = tuple(settings.owner_ids)

    async def _send(text: str) -> None:
        for chat_id in targets:
            await bot.send_message(chat_id, text, parse_mode="HTML")

    alerts.configure(_send)
