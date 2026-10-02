"""
Audit jurnali — har bir nozik admin amali uchun yagona yozuv formati.

NIMA YOZILADI (talab)
---------------------
* actor (Telegram ID) va uning roli;
* amal nomi (``admin_added``, ``role_changed``, ``user_banned``, ...);
* target (foydalanuvchi/ulanish/sozlama);
* natija (``ok`` / ``rejected``);
* eski va yangi qiymat (imkon bo'lsa).

QANDAY SAQLANADI
----------------
Yangi jadval/ustun QO'SHILMADI: mavjud ``activity_log`` (event_type,
description, user_id, severity, occurred_at) ishlatiladi — shu tufayli SQLite
va Postgres uchun bir xil, migratsiya kerak emas va admin panelning
"📋 Activity Logs" ekrani o'zgarishsiz ko'rsatadi.

MAXFIY MA'LUMOT YO'ZILMAYDI: token, parol, DSN — hech qachon.  Faqat
identifikatorlar, rollar va qisqa qiymatlar (``description`` 400 belgiga
qisqartiriladi).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

MAX_DETAIL = 400


def _short(value: Any) -> str:
    """Qiymatni xavfsiz qisqa satrga aylantiradi (``None`` -> ``-``)."""
    if value is None or value == "":
        return "-"
    text = str(value).replace("\n", " ").strip()
    return text[:120] or "-"


def format_entry(
    action: str,
    *,
    actor: int = 0,
    actor_role: Optional[str] = None,
    target: Any = None,
    result: str = "ok",
    old: Any = None,
    new: Any = None,
    details: str = "",
) -> str:
    """Audit yozuvining matni (testlar ham AYNAN shu formatni tekshiradi)."""
    parts = [
        f"action={action}",
        f"actor={actor or '-'}",
        f"role={_short(actor_role)}",
        f"target={_short(target)}",
        f"result={_short(result)}",
    ]
    if old is not None or new is not None:
        parts.append(f"old={_short(old)}")
        parts.append(f"new={_short(new)}")
    if details:
        parts.append(f"details={_short(details)}")
    return " ".join(parts)[:MAX_DETAIL]


async def log_action(
    action: str,
    *,
    actor: int = 0,
    actor_role: Optional[str] = None,
    target: Any = None,
    result: str = "ok",
    old: Any = None,
    new: Any = None,
    details: str = "",
    severity: str = "INFO",
    connection_id: Optional[str] = None,
) -> None:
    """Audit yozuvini activity logga qo'shadi.

    Jurnal yozilmasa ham AMAL TO'XTAMAYDI (audit xatosi biznes-amalni
    buzmasligi kerak) — sabab logga tushadi.
    """
    role = actor_role
    if role is None and actor:
        try:
            from app.services import admin_roles

            role = admin_roles.role_of(actor)
        except Exception:  # noqa: BLE001 – rol aniqlanmasa ham yozamiz
            role = None
    text = format_entry(
        action,
        actor=actor,
        actor_role=role,
        target=target,
        result=result,
        old=old,
        new=new,
        details=details,
    )
    try:
        from app.database import db

        await db.add_activity_log(
            action,
            text,
            connection_id=connection_id,
            user_id=actor or None,
            severity=severity,
        )
    except Exception:  # noqa: BLE001
        logger.warning("Audit yozuvi saqlanmadi: %s", text, exc_info=True)
