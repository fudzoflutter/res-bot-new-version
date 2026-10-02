"""
Moderatsiya — ban / unban (server-side, DB'da saqlanadi).

NEGA ALOHIDA MODUL
------------------
Botning ESKI "foydalanuvchi ruxsati" tizimi (pending/allowed/denied/banned
+ /allow va /ban buyruqlari) ataylab OLIB TASHLANGAN edi.  Shu sababli qayta
ishlash uchun uning API'si TIKLANMADI — o'rniga KICHIK, ANIQ moderatsiya
qatlami qo'shildi:

* faqat ban/unban (kirishni to'liq bloklash emas, balki aniq ro'yxat);
* ``bot_settings`` dagi ``banned_users`` kaliti (JSON) — yangi jadval kerak
  emas, SQLite va Postgres uchun bir xil;
* har bir amal ``users.moderate`` ruxsati bilan himoyalangan
  (:mod:`app.services.permissions`) va audit jurnaliga yoziladi.

BAN QILINGAN FOYDALANUVCHI update'i middleware darajasida to'xtatiladi
(:mod:`app.middlewares`) — admin/owner hech qachon bloklanmaydi.
"""

from __future__ import annotations

from app.utils.timeutils import local_now
from app.utils.formatting import esc
from app.utils.telegram_api import call as tg_call

import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

SETTING_KEY = "banned_users"
LOAD_ATTEMPTS = 3
LOAD_RETRY_SECONDS = 0.5

# {user_id: {"by": int, "at": iso, "reason": str}}
_banned: dict[int, dict] = {}
_loaded = False
_bot = None
_lock = asyncio.Lock()


def configure_notifier(bot) -> None:
    global _bot
    _bot = bot


async def _notify_user(user_id: int, text: str) -> bool:
    if _bot is None:
        return False
    try:
        await tg_call(
            "moderation_notify",
            lambda: _bot.send_message(user_id, text, parse_mode="HTML"),
            attempts=2,
        )
        return True
    except Exception:
        logger.info("Moderation notification failed (user=%s)", user_id, exc_info=True)
        return False


def _parse(raw: str) -> dict[int, dict]:
    """JSON yozuvni xavfsiz o'qiydi (buzuq bo'lsa — bo'sh)."""
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    parsed: dict[int, dict] = {}
    for key, value in data.items():
        try:
            user_id = int(key)
        except (TypeError, ValueError):
            continue
        if user_id <= 0:
            continue
        entry = value if isinstance(value, dict) else {}
        parsed[user_id] = {
            "by": int(entry.get("by") or 0),
            "at": str(entry.get("at") or ""),
            "reason": str(entry.get("reason") or "")[:200],
        }
    return parsed


async def load() -> None:
    """Ban ro'yxatini bazadan o'qiydi (startupda).

    Moderatsiya xavfsizlik chegarasi bo'lgani uchun DB holati noma'lum bo'lsa
    bot ``banned_users = {}`` deb fail-open qilmaydi. Bir necha marta qayta
    urinadi; baribir o'qilmasa startup xato bilan to'xtaydi.
    """
    global _banned, _loaded
    from app.database import db

    last_error: Exception | None = None
    for attempt in range(1, LOAD_ATTEMPTS + 1):
        try:
            raw = await db.get_setting(SETTING_KEY, "{}")
            _banned = _parse(raw)
            _loaded = True
            if _banned:
                logger.info("Ban ro'yxati yuklandi: %s ta", len(_banned))
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning(
                "Ban ro'yxati o'qilmadi (urinish %s/%s)",
                attempt, LOAD_ATTEMPTS, exc_info=True,
            )
            if attempt < LOAD_ATTEMPTS:
                await asyncio.sleep(LOAD_RETRY_SECONDS * attempt)

    _loaded = False
    raise RuntimeError(
        "Ban ro'yxatini bazadan o'qib bo'lmadi; fail-open oldini olish uchun startup to'xtatildi"
    ) from last_error


async def _persist(snapshot: Optional[dict[int, dict]] = None) -> None:
    from app.database import db

    data = _banned if snapshot is None else snapshot
    payload = json.dumps({str(k): v for k, v in sorted(data.items())})
    await db.set_setting(SETTING_KEY, payload)


def is_banned(user_id: int) -> bool:
    """``user_id`` ban ro'yxatidami? (in-memory, tez)."""
    return int(user_id or 0) in _banned


def status(user_id: int) -> Optional[dict]:
    """Ban yozuvi (bo'lmasa ``None``)."""
    entry = _banned.get(int(user_id or 0))
    return dict(entry) if entry else None


def count_banned() -> int:
    """Ban qilinganlar soni (analytics "Banned Users" ko'rsatkichi)."""
    return len(_banned)


def list_banned(limit: Optional[int] = None, offset: int = 0) -> list[dict]:
    """Ban ro'yxati (eng yangisi birinchi)."""
    rows = [
        {"user_id": user_id, **entry}
        for user_id, entry in _banned.items()
    ]
    rows.sort(key=lambda row: (row.get("at") or "", row["user_id"]), reverse=True)
    if offset:
        rows = rows[offset:]
    if limit is not None:
        rows = rows[: max(0, int(limit))]
    return rows


async def ban(user_id: int, *, by: int = 0, reason: str = "") -> bool:
    """Foydalanuvchini ban qiladi va darhol shaxsiy chatiga xabar yuboradi.

    DB yozuvi muvaffaqiyatsiz bo'lsa in-memory holat O'ZGARTIRILMAYDI.
    Shu bilan restartdan keyingi "RAM banned / DB unbanned" driftining oldi olinadi.
    """
    global _banned
    user_id = int(user_id or 0)
    if user_id <= 0:
        return False
    async with _lock:
        if user_id in _banned:
            return False
        entry = {
            "by": int(by or 0),
            "at": local_now().isoformat(timespec="seconds"),
            "reason": str(reason or "")[:200],
        }
        snapshot = dict(_banned)
        snapshot[user_id] = entry
        try:
            await _persist(snapshot)
        except Exception:
            logger.error("Ban DBga saqlanmadi; in-memory holat o'zgartirilmadi (user=%s)", user_id, exc_info=True)
            return False
        _banned = snapshot
    try:
        from app.utils import texts
        msg = texts.USER_BANNED
        if reason:
            msg += f"\n\n<b>Sabab:</b> {esc(str(reason)[:200])}"
        await _notify_user(user_id, msg)
    except Exception:
        logger.debug("Ban notification preparation failed", exc_info=True)
    return True


async def unban(user_id: int) -> bool:
    """Ban'ni olib tashlaydi va darhol foydalanuvchiga xabar yuboradi."""
    global _banned
    user_id = int(user_id or 0)
    async with _lock:
        if user_id not in _banned:
            return False
        snapshot = dict(_banned)
        snapshot.pop(user_id, None)
        try:
            await _persist(snapshot)
        except Exception:
            logger.error("Unban DBga saqlanmadi; in-memory holat o'zgartirilmadi (user=%s)", user_id, exc_info=True)
            return False
        _banned = snapshot
    try:
        from app.utils import texts
        await _notify_user(user_id, texts.USER_UNBANNED)
    except Exception:
        logger.debug("Unban notification preparation failed", exc_info=True)
    return True


async def remove_ban_without_notification(user_id: int) -> bool:
    """User o'chirilayotganda ban yozuvini jim olib tashlaydi va saqlaydi."""
    global _banned
    user_id = int(user_id or 0)
    async with _lock:
        if user_id not in _banned:
            return False
        snapshot = dict(_banned)
        snapshot.pop(user_id, None)
        try:
            await _persist(snapshot)
        except Exception:
            logger.error("User delete paytida ban holati saqlanmadi (user=%s)", user_id, exc_info=True)
            return False
        _banned = snapshot
        return True


def reset_for_tests() -> None:
    """Testlar uchun holatni tozalash."""
    global _banned, _loaded, _bot
    _banned = {}
    _loaded = False
    _bot = None
