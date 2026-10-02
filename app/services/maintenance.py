"""
Persistent hold/maintenance mode with mandatory all-user notifications.

The DB value is authoritative. Every real ON/OFF transition triggers a
best-effort notification to every registered bot user. Notifications are sent
in bounded concurrent batches so a large user base does not block the event
loop with thousands of sequential sends.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from app.utils.telegram_api import call as tg_call
from app.utils.tasks import spawn

logger = logging.getLogger(__name__)

SETTING_KEY = "maintenance_mode"
# Kept for backwards compatibility with older code/tests; no longer user-toggled.
RESUME_NOTIFY_KEY = "resume_notify"
CACHE_TTL_SECONDS = 15.0
NOTIFY_BATCH_SIZE = 50
USER_ID_BATCH = 500

_cache: dict[str, Any] = {"value": False, "at": 0.0, "loaded": False}
_bot: Optional[Any] = None
_notify_task: Optional[asyncio.Task] = None


def configure_notifier(bot: Any) -> None:
    global _bot
    _bot = bot


def invalidate() -> None:
    _cache["at"] = 0.0


def reset_for_tests() -> None:
    global _bot, _notify_task
    if _notify_task and not _notify_task.done():
        _notify_task.cancel()
    _notify_task = None
    _cache["value"], _cache["at"], _cache["loaded"] = False, 0.0, False
    _bot = None


async def is_enabled(*, force: bool = False) -> bool:
    now = time.monotonic()
    if not force and _cache["at"] and (now - _cache["at"]) < CACHE_TTL_SECONDS:
        return bool(_cache["value"])
    from app.database import db
    try:
        value = (await db.get_setting(SETTING_KEY, "0")) == "1"
    except Exception:  # noqa: BLE001
        # DB xatosida Hold Mode'ni o'z-o'zidan OFF qilish xavfli. Oldin
        # muvaffaqiyatli o'qilgan holat bo'lsa o'shani saqlaymiz; birinchi
        # o'qishning o'zi muvaffaqiyatsiz bo'lsa fail-closed sifatida ON.
        if _cache.get("loaded"):
            logger.warning(
                "Hold mode holatini o'qib bo'lmadi — oxirgi ma'lum holat saqlandi (%s)",
                "ON" if _cache["value"] else "OFF",
                exc_info=True,
            )
            _cache["at"] = now
            return bool(_cache["value"])
        logger.error(
            "Hold mode holati birinchi o'qishda noma'lum — xavfsizlik uchun ON",
            exc_info=True,
        )
        # ``loaded=False`` paytida TTL qo'ymaymiz: keyingi chaqiriq DBni
        # darhol qayta sinaydi, lekin shu chaqiriq baribir fail-closed ON.
        _cache["value"], _cache["at"], _cache["loaded"] = True, 0.0, False
        return True
    _cache["value"], _cache["at"], _cache["loaded"] = value, now, True
    return value


async def resume_notification_enabled() -> bool:
    """Compatibility API: maintenance notifications are mandatory now."""
    return True


async def _send_one(uid: int, text: str) -> bool:
    if _bot is None:
        return False
    try:
        await tg_call(
            "maintenance_notify",
            lambda chat_id=uid: _bot.send_message(chat_id, text, parse_mode="HTML"),
            attempts=3,
        )
        return True
    except Exception:
        logger.info("Maintenance notification failed (user=%s)", uid, exc_info=True)
        return False


async def notify_state_change_to_users(enabled: bool) -> dict[str, int]:
    """Notify every registered user in bounded concurrent batches."""
    if _bot is None:
        return {"total": 0, "sent": 0, "failed": 0}
    from app.database import db
    from app.utils import texts

    text = texts.HOLD_MODE if enabled else texts.HOLD_RESUMED
    total = sent = failed = 0
    offset = 0
    while True:
        recipients = await db.all_user_ids(limit=USER_ID_BATCH, offset=offset)
        if not recipients:
            break
        offset += len(recipients)
        total += len(recipients)
        for start in range(0, len(recipients), NOTIFY_BATCH_SIZE):
            chunk = recipients[start:start + NOTIFY_BATCH_SIZE]
            results = await asyncio.gather(*(_send_one(uid, text) for uid in chunk), return_exceptions=True)
            sent += sum(1 for result in results if result is True)
            failed += len(results) - sum(1 for result in results if result is True)
    try:
        await db.add_activity_log(
            "maintenance_notification",
            f"Maintenance {'ON' if enabled else 'OFF'} notification: total={total}, sent={sent}, failed={failed}",
            severity="WARNING",
        )
    except Exception:
        logger.debug("Maintenance notification activity log failed", exc_info=True)
    logger.info("Maintenance notification finished: total=%s sent=%s failed=%s", total, sent, failed)
    return {"total": total, "sent": sent, "failed": failed}


async def notify_resume_to_users() -> int:
    """Compatibility wrapper — notifies all registered users."""
    result = await notify_state_change_to_users(False)
    return int(result.get("sent", 0))


async def _notify_background(enabled: bool) -> None:
    try:
        await notify_state_change_to_users(enabled)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Maintenance notification worker failed")


async def _notify_after_previous(previous: asyncio.Task, enabled: bool) -> None:
    try:
        await previous
    except asyncio.CancelledError:
        # Previous notification was interrupted; continue with the latest state.
        pass
    except Exception:
        logger.exception("Previous maintenance notification worker failed")
    await _notify_background(enabled)


async def set_enabled(
    enabled: bool,
    *,
    notify_resume: bool = True,
    await_notifications: bool = False,
) -> Optional[dict[str, int]]:
    """Persist hold state and notify on every real ON/OFF transition.

    ``await_notifications=True`` is intended for deterministic tests. The
    normal admin-panel path schedules the notification worker immediately so
    the settings request does not block on a potentially large recipient set.
    """
    global _notify_task
    from app.database import db

    was_enabled = await is_enabled(force=True)
    await db.set_setting(SETTING_KEY, "1" if enabled else "0")
    _cache["value"], _cache["at"], _cache["loaded"] = bool(enabled), time.monotonic(), True
    if enabled:
        logger.warning("HOLD MODE YOQILDI — oddiy foydalanuvchi amallari to'xtatildi")
    else:
        logger.info("HOLD MODE O'CHIRILDI — normal ish qaytdi")

    if was_enabled == bool(enabled) or not notify_resume:
        return None

    if await_notifications:
        return await notify_state_change_to_users(bool(enabled))

    previous = _notify_task if _notify_task and not _notify_task.done() else None
    if previous is not None:
        logger.warning("Maintenance notification worker already running; queuing latest transition")
        _notify_task = spawn(
            _notify_after_previous(previous, bool(enabled)),
            name="maintenance-notifications-queued",
        )
    else:
        _notify_task = spawn(
            _notify_background(bool(enabled)), name="maintenance-notifications"
        )
    return {"total": 0, "sent": 0, "failed": 0, "scheduled": 1}
