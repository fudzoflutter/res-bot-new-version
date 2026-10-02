"""
Admin rollari (OWNER / ADMIN / MODERATOR / VIEWER) — server-side.

ARXITEKTURA
-----------
* **OWNER**     — ``ADMIN_ID`` (+ ixtiyoriy ``ADMIN_OWNER_IDS``): barcha
  huquqlar, admin boshqaruvi, destruktiv amallar.
* **ADMIN**     — ``ADMIN_IDS`` env yoki panel orqali qo'shilganlar: deyarli
  barcha operatsion amallar (OWNER-only amallar bundan mustasno).
* **MODERATOR** — ko'rish + moderatsiya (ban/unban).
* **VIEWER**    — faqat o'qish (analytics/statistika).

Runtime rollar ``bot_settings`` jadvalida (``admin_roles`` kaliti, JSON)
saqlanadi — bot qayta ishga tushsa ham qoladi.  OWNERlar FAQAT env orqali
o'zgartiriladi (runtime'da owner qo'shib bo'lmaydi — xavfsizlik chegarasi),
shu sababli "kamida bitta OWNER qoladi" sharti HAR DOIM bajariladi.

Ruxsatlarning o'zi :mod:`app.services.permissions` da (yagona matritsa).
Barcha tekshiruvlar SERVER-SIDE: tugmani yashirish yetarli emas.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from app.config import settings
from app.services import permissions as perms

logger = logging.getLogger(__name__)

ROLE_OWNER = perms.ROLE_OWNER
ROLE_ADMIN = perms.ROLE_ADMIN
ROLE_MODERATOR = perms.ROLE_MODERATOR
ROLE_VIEWER = perms.ROLE_VIEWER

#: Orqaga moslik: avval faqat ikki rol bor edi.
VALID_ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_MODERATOR, ROLE_VIEWER)
MANAGED_ROLES = perms.MANAGED_ROLES

SETTING_KEY = "admin_roles"

# {user_id: role} — faqat runtime (env'dan tashqari) qo'shilganlar.
_extra: dict[int, str] = {}
_loaded = False
_lock = asyncio.Lock()


def _parse(raw: str) -> dict[int, str]:
    """JSON yozuvni xavfsiz o'qiydi (buzuq bo'lsa — bo'sh)."""
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    parsed: dict[int, str] = {}
    for key, value in data.items():
        try:
            user_id = int(key)
        except (TypeError, ValueError):
            continue
        role = perms.normalize_role(value)
        # OWNER runtime'dan tiklanmaydi (faqat env) — xavfsizlik chegarasi.
        if role in MANAGED_ROLES and user_id > 0:
            parsed[user_id] = role
    return parsed


async def load() -> None:
    """Sozlamadan runtime rollarni o'qiydi (startupda va o'zgarishdan keyin)."""
    global _extra, _loaded
    from app.database import db

    try:
        raw = await db.get_setting(SETTING_KEY, "{}")
    except Exception:  # noqa: BLE001 – DB xatosi admin panelni buzmasin
        logger.warning("Admin rollari o'qilmadi (DB xatosi)", exc_info=True)
        return
    _extra = _parse(raw)
    _loaded = True
    if _extra:
        logger.info("Runtime adminlar yuklandi: %s", len(_extra))


def ensure_loaded() -> None:
    """Sinxron ishlatish uchun ogohlantirish (load() hali chaqirilmagan)."""
    if not _loaded:
        logger.debug("Admin rollari hali yuklanmagan (load() chaqirilmagan)")


def role_of(user_id: int) -> Optional[str]:
    """Rolni aniqlaydi: env owner -> runtime rol -> env admin."""
    env_role = settings.role_of(user_id)
    if env_role is not None:
        return env_role
    return _extra.get(user_id)


def is_admin(user_id: int) -> bool:
    """Har qanday panel roli (owner/admin/moderator/viewer)."""
    return role_of(user_id) is not None


def is_owner(user_id: int) -> bool:
    return role_of(user_id) == ROLE_OWNER


def has_permission(user_id: int, permission: str) -> bool:
    """Server-side ruxsat tekshiruvi (markazlashtirilgan matritsa orqali)."""
    return perms.role_has(role_of(user_id), permission)


def _serialize() -> str:
    return json.dumps({str(k): v for k, v in sorted(_extra.items())})


async def _persist(snapshot: Optional[dict[int, str]] = None) -> None:
    from app.database import db

    data = _extra if snapshot is None else snapshot
    await db.set_setting(SETTING_KEY, json.dumps({str(k): v for k, v in sorted(data.items())}))


def owner_count() -> int:
    """Hozirgi OWNERlar soni (env + runtime; runtime owner bo'lmaydi)."""
    return len(settings.owner_ids)


def owner_would_remain(user_id: int) -> bool:
    """``user_id`` olib tashlansa/yaxshilanmasa kamida bitta OWNER qoladimi?"""
    import app.config  # noqa: F401 – izoh: env o'zgarmaydi, hisob tekshiriladi

    if user_id not in settings.owner_ids:
        return owner_count() >= 1
    # env'dan owner o'chirib bo'lmaydi — lekin himoya shartini baribir tekshiramiz.
    return owner_count() - 1 >= 1


def _env_role(user_id: int) -> Optional[str]:
    return settings.role_of(user_id)


async def add(user_id: int, role: str = ROLE_ADMIN) -> bool:
    """Runtime rol beradi (faqat MANAGED_ROLES; ownerlar env'dan).

    Qaytaradi ``True`` — qo'shildi/yangilandi, ``False`` — rad etildi.
    """
    global _extra
    normalized = perms.normalize_role(role)
    if normalized not in MANAGED_ROLES or user_id <= 0:
        logger.warning("Runtime rol rad etildi: user=%s role=%s", user_id, role)
        return False
    if _env_role(user_id) is not None:
        return False  # env orqali allaqachon rol berilgan
    async with _lock:
        if user_id in _extra:
            return False
        snapshot = dict(_extra)
        snapshot[user_id] = normalized
        try:
            await _persist(snapshot)
        except Exception:
            logger.error("Admin role saqlanmadi (user=%s role=%s)", user_id, normalized, exc_info=True)
            return False
        _extra = snapshot
    return True


async def set_role(user_id: int, role: str) -> bool:
    """Mavjud runtime rolni o'zgartiradi (env rollariga tegmaydi).

    * OWNER rolini berib bo'lmaydi (env-only chegarasi);
    * env orqali belgilangan admin/owner o'zgartirilmaydi;
    * kamida bitta OWNER qolishi kafolatlanadi.
    """
    global _extra
    normalized = perms.normalize_role(role)
    if normalized not in MANAGED_ROLES or user_id <= 0:
        return False
    if not owner_would_remain(user_id):
        logger.error("OWNER himoyasi: oxirgi owner yaxshilanmasligi kerak edi")
        return False
    if _env_role(user_id) is not None:
        return False  # env roli panel orqali o'zgarmaydi

    async with _lock:
        current = _extra.get(user_id)
        if current == normalized:
            return False
        snapshot = dict(_extra)
        snapshot[user_id] = normalized
        try:
            await _persist(snapshot)
        except Exception:
            logger.error("Admin role change saqlanmadi (user=%s role=%s)", user_id, normalized, exc_info=True)
            return False
        _extra = snapshot
    return True


async def remove(user_id: int) -> bool:
    """Runtime rolni olib tashlaydi (env owner/adminlar tegmaydi)."""
    global _extra
    if user_id not in _extra:
        return False  # env orqali belgilangan rolni o'chirib bo'lmaydi
    if _env_role(user_id) == ROLE_OWNER or not owner_would_remain(user_id):
        logger.error("OWNER himoyasi: %s olib tashlanmadi", user_id)
        return False
    async with _lock:
        if user_id not in _extra:
            return False
        snapshot = dict(_extra)
        snapshot.pop(user_id, None)
        try:
            await _persist(snapshot)
        except Exception:
            logger.error("Admin role remove saqlanmadi (user=%s)", user_id, exc_info=True)
            return False
        _extra = snapshot
    return True


def has_runtime(user_id: int) -> bool:
    """True when user_id is a runtime role holder (panel-added)."""
    return user_id in _extra


def list_admins() -> dict[int, str]:
    """Barcha rollar: env + runtime (role bilan)."""
    result: dict[int, str] = {}
    for user_id in settings.owner_ids:
        result[int(user_id)] = ROLE_OWNER
    for user_id in settings.admin_ids_extra:
        result.setdefault(int(user_id), ROLE_ADMIN)
    for user_id, role in _extra.items():
        result.setdefault(int(user_id), role)
    return result


def runtime_admins() -> dict[int, str]:
    """Faqat panel orqali qo'shilganlar (o'chirish mumkin bo'lganlar)."""
    return dict(_extra)


def role_label(user_id: int) -> str:
    """Panel/UI uchun ikonka + rol nomi."""
    role = role_of(user_id)
    if role is None:
        return "—"
    icons = {
        ROLE_OWNER: "👑",
        ROLE_ADMIN: "🛡",
        ROLE_MODERATOR: "⚖️",
        ROLE_VIEWER: "👁",
    }
    return f"{icons.get(role, '•')} {role}"


def reset_for_tests() -> None:
    """Testlar uchun runtime holatni tozalash."""
    global _extra, _loaded
    _extra = {}
    _loaded = False
