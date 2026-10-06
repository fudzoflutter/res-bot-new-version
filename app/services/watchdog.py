"""
Watchdog — fon vazifalari nazoratchisi (health monitoring bilan).

VAZIFALAR
---------
* **Retention cleanup** — ``retention_days`` sozlamasiga ko'ra eski
  hodisalar va eski activity log yozuvlarini o'chiradi (SQLite/Postgres
  indekslari shu so'rovlar uchun optimallashtirilgan).
* **RAM kesh** — TTL bo'yicha eskirgan yozuvlarni tozalaydi
  (:func:`app.services.reporter.purge_expired`).
* **DB health check** — ``db.ping()`` — baza javob bermasa holat 🔴 bo'ladi.
* **Xatolarni yashirmaydi** — har bir sikl natijasi holatga yoziladi;
  ketma-ket xatolar (``FAILURE_ALERT_THRESHOLD``) adminga ALERT bo'lib
  boradi va activity logga tushadi.

Watchdog O'ZI ham admin panel "🤖 Bot Health" ekranida ko'rinadi
(:func:`status`) — "🟢 Running" yoki "🔴 ERROR" ko'rinishida.

Muhim: bu modul hech qachon botni to'xtatmaydi — xatolar log qilinadi va
keyingi siklda qayta uriniladi (supervisor uni baribir qayta ishga tushiradi,
:mod:`app.main`).
"""

from __future__ import annotations

from app.emoji_config import EMOJI

import asyncio
import logging
import time
from typing import Optional

from app.database import db
from app.services import alerts

logger = logging.getLogger(__name__)

# Yengil tekshiruv har shuncha sekundda (DB ping + navbat).
TICK_SECONDS = 60.0
# Og'ir housekeeping har shuncha sekundda bir marta (retention + kesh).
HOUSEKEEPING_SECONDS = 15 * 60.0
# Nechta ketma-ket xatodan keyin adminga alert yuboriladi.
FAILURE_ALERT_THRESHOLD = 3
# Standart retention (kun) — sozlama bo'sh bo'lsa.
DEFAULT_RETENTION_DAYS = 90
CONNECTION_VERIFY_SECONDS = 60.0
_last_connection_verify: Optional[float] = None


class WatchdogState:
    """Watchdog holati (admin panel uchun, xotira ichida)."""

    def __init__(self) -> None:
        self.running: bool = False
        self.started_at: Optional[float] = None
        self.last_tick_at: Optional[float] = None
        self.cycles: int = 0
        self.failures: int = 0
        self.consecutive_failures: int = 0
        self.crashes: int = 0
        self.last_error: str = ""
        self.housekeeping_runs: int = 0
        self.last_housekeeping_at: Optional[float] = None
        self.pruned_events: int = 0
        self.pruned_logs: int = 0
        self.purged_cache: int = 0
        self.retention_days: int = DEFAULT_RETENTION_DAYS

    # -- holat yozuvlari ----------------------------------------------------
    def start(self) -> None:
        self.running = True
        self.started_at = time.monotonic()

    def stop(self) -> None:
        self.running = False

    def record_ok(self) -> None:
        self.cycles += 1
        self.last_tick_at = time.monotonic()
        self.consecutive_failures = 0

    def record_error(self, exc: BaseException) -> None:
        self.failures += 1
        self.consecutive_failures += 1
        self.last_error = f"{type(exc).__name__}: {exc}"[:200]
        self.last_tick_at = time.monotonic()

    def record_crash(self, exc: BaseException) -> None:
        self.crashes += 1
        self.last_error = f"crash: {type(exc).__name__}: {exc}"[:200]

    # -- o'qish -------------------------------------------------------------
    @property
    def status_icon(self) -> str:
        if not self.running:
            return EMOJI.offline_dot.plain
        if self.consecutive_failures >= FAILURE_ALERT_THRESHOLD:
            return EMOJI.offline_dot.plain
        if self.consecutive_failures:
            return EMOJI.warning.plain
        return EMOJI.online_dot.plain

    @property
    def status_text(self) -> str:
        if not self.running:
            return "STOPPED"
        if self.consecutive_failures >= FAILURE_ALERT_THRESHOLD:
            return f"ERROR (x{self.consecutive_failures})"
        if self.consecutive_failures:
            return f"WARN ({self.consecutive_failures} xato)"
        return "Running"

    def _age(self, stamp: Optional[float]) -> str:
        if stamp is None:
            return "—"
        seconds = max(0, int(time.monotonic() - stamp))
        if seconds < 60:
            return f"{seconds}s oldin"
        if seconds < 3600:
            return f"{seconds // 60}m oldin"
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m oldin"

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "status_icon": self.status_icon,
            "status_text": self.status_text,
            "cycles": self.cycles,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "crashes": self.crashes,
            "last_tick": self._age(self.last_tick_at),
            "housekeeping_runs": self.housekeeping_runs,
            "last_housekeeping": self._age(self.last_housekeeping_at),
            "pruned_events": self.pruned_events,
            "pruned_logs": self.pruned_logs,
            "purged_cache": self.purged_cache,
            "retention_days": self.retention_days,
            "last_error": self.last_error or "—",
        }


state = WatchdogState()


async def _retention_days() -> int:
    """Sozlamadagi retention kunini o'qiydi (xato bo'lsa standart)."""
    try:
        raw = await db.get_setting("retention_days", str(DEFAULT_RETENTION_DAYS))
        days = int(str(raw).strip())
    except Exception:  # noqa: BLE001 – sozlama o'qilmasa standart
        return DEFAULT_RETENTION_DAYS
    if days not in (7, 30, 90, 180):
        return DEFAULT_RETENTION_DAYS
    return days


async def housekeeping() -> None:
    """Davriy tozalash: retention + RAM kesh (xatolar yuqoriga ko'tariladi)."""
    from app.services import reporter

    days = await _retention_days()
    state.retention_days = days

    removed_events = await db.prune_events_older_than(days)
    removed_logs = await db.prune_activity_logs_older_than(days)
    purged = reporter.purge_expired()

    state.housekeeping_runs += 1
    state.last_housekeeping_at = time.monotonic()
    state.pruned_events += int(removed_events or 0)
    state.pruned_logs += int(removed_logs or 0)
    state.purged_cache += int(purged or 0)
    logger.info(
        "Watchdog housekeeping: events-%s, logs-%s, cache-%s (retention=%s kun)",
        removed_events, removed_logs, purged, days,
    )
    # Jurnalga qisqa yozuv (faqat amalda o'chirish bo'lsa — jurnal to'lmasin).
    if (removed_events or removed_logs) and hasattr(db, "add_activity_log"):
        try:
            await db.add_activity_log(
                "retention_cleanup",
                f"Retention {days} kun: {removed_events} hodisa, "
                f"{removed_logs} log o'chirildi",
                severity="INFO",
            )
        except Exception:  # noqa: BLE001 – jurnal xatosi tozalashni buzmaydi
            logger.debug("Retention log yozilmadi", exc_info=True)


async def _alert_failures() -> None:
    """Ketma-ket xatolar bo'yicha adminga BIR marta alert yuboradi."""
    if state.consecutive_failures < FAILURE_ALERT_THRESHOLD:
        return
    await alerts.alerts.notify(
        "watchdog_failure",
        "WATCHDOG XATOSI",
        f"Watchdog ketma-ket {state.consecutive_failures} marta xato berdi.\n"
        f"Oxirgi xato: {state.last_error}\n"
        f"Jami sikl: {state.cycles}, xatolar: {state.failures}\n"
        f"Tekshiring: {EMOJI.database.plain} Database va {EMOJI.activity_log.plain} Activity Logs.",
        severity=alerts.SEVERITY_ERROR,
        also_log=False,
    )


async def _tick() -> None:
    """Bitta yengil sikl: DB health + housekeeping + connection reconciliation."""
    global _last_connection_verify
    try:
        alive = await db.ping()
        if not alive:
            raise RuntimeError("DB ping javob bermadi")
        if (
            state.last_housekeeping_at is None
            or time.monotonic() - state.last_housekeeping_at >= HOUSEKEEPING_SECONDS
        ):
            await housekeeping()
        now = time.monotonic()
        if _last_connection_verify is None or now - _last_connection_verify >= CONNECTION_VERIFY_SECONDS:
            try:
                from app.services import connection_verify
                result = await connection_verify.verify_all(force=False, notify=True)
                _last_connection_verify = now
                if result.get("changed"):
                    logger.warning("Connection reconciliation: %s", result)
            except Exception:
                logger.exception("Connection reconciliation cycle failed")
    except Exception as exc:  # noqa: BLE001 – holatga yozamiz va davom
        state.record_error(exc)
        logger.exception("Watchdog sikli xato berdi")
        try:
            await db.add_activity_log(
                "watchdog_error",
                f"Watchdog xatosi: {type(exc).__name__}",
                severity="ERROR",
            )
        except Exception:  # noqa: BLE001
            pass
        await _alert_failures()
        return
    state.record_ok()


async def run_watchdog() -> None:
    """Asosiy tsikl — :mod:`app.main` supervisor uni ushlab turadi."""
    state.start()
    logger.info(
        "Watchdog ishga tushdi (tick=%ss, housekeeping=%ss)",
        int(TICK_SECONDS), int(HOUSEKEEPING_SECONDS),
    )
    try:
        while True:
            await _tick()
            await asyncio.sleep(TICK_SECONDS)
    finally:
        state.stop()


async def notify_crash(exc: BaseException) -> None:
    """Supervisor watchdog'ni qayta ishga tushirayotganda chaqiradi."""
    state.record_crash(exc)
    await alerts.alerts.notify(
        "watchdog_crash",
        "WATCHDOG QAYTA ISHGA TUSHDI",
        f"Watchdog kutilmagan tarzda to'xtadi va 30 soniyadan keyin qayta "
        f"ishga tushadi.\nXato: {type(exc).__name__}: {exc}\n"
        f"Jami qayta ishga tushishlar: {state.crashes}",
        severity=alerts.SEVERITY_CRITICAL,
        also_log=True,
    )


def status() -> dict:
    """Admin panel uchun qisqa holat (health ekrani)."""
    snapshot = state.snapshot()
    snapshot["next_housekeeping_in"] = (
        "—"
        if state.last_housekeeping_at is None
        else f"{max(0, int(HOUSEKEEPING_SECONDS - (time.monotonic() - state.last_housekeeping_at)))}s"
    )
    return snapshot


