"""
Web Admin Panel API (aiohttp) — xavfsiz, server-side avtorizatsiya bilan.

XAVFSIZLIK QOIDALARI
--------------------
1. Har bir himoyalangan endpoint Telegram WebApp ``initData`` ni TO'LIQ
   tekshiradi (HMAC-SHA256, bot token bilan).  «Header mavjudmi» tekshirish
   YETARLI EMAS — ``X-Telegram-Init-Data: test`` kabi soxta qiymat HECH QACHON
   autentifikatsiya qilmaydi.
2. initData ichidagi ``user.id`` serverda aniqlanadi va rol
   (owner/admin/moderator/viewer) :mod:`app.services.admin_roles` orqali qayta
   tekshiriladi.  Frontendda yuborilgan ``isAdmin`` / ``isOwner`` / ``role``
   qiymatlariga ISHONILMAYDI.
3. Har bir endpoint ANIQ ruxsat talab qiladi (markazlashtirilgan matritsa:
   :mod:`app.services.permissions`) — ruxsat yo'q bo'lsa HTTP 403.
4. Broadcast FAQAT qabul qilingandan keyin (fon vazifasida) boshlanadi va
   natija alohida status endpoint orqali olinadi — «yuborildi» deb erta xabar
   berilmaydi.  Telegram 429/network xatolari ``telegram_api.call`` orqali
   qayta uriniladi.  Bir vaqtda faqat BITTA broadcast ishlaydi (takroriy
   bosish 409 qaytaradi) va progress bazada saqlanadi.

Broadcast dvigatelining o'zi — :mod:`app.services.broadcast` (eski nomlar
``_broadcast_state`` / ``broadcast_snapshot`` / ``recover_broadcast_state``
moslik uchun shu yerda ham ko'rinadi).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Optional
from urllib.parse import parse_qsl

from aiohttp import web

from app.config import settings
from app.database import db
from app.services import admin_roles, analytics, audit, connection_verify, maintenance, moderation
from app.services import broadcast as broadcast_engine
from app.services import permissions as perms
from app.services.broadcast import AdError, BroadcastBusy
from app.utils.telegram_api import call as tg_call

logger = logging.getLogger(__name__)

# initData qancha vaqt amal qiladi (Telegram tavsiyasi: 24 soat).
INITDATA_MAX_AGE = 86400

_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


# ---------------------------------------------------------------------------
# Telegram WebApp initData tekshiruvi
# ---------------------------------------------------------------------------
def parse_init_data(init_data: str) -> dict[str, str]:
    """``a=1&b=2`` ko'rinishidagi initData'ni URL-decode qilib dict ga aylantiradi.

    Telegram initData — URL-encoded query string (``user=%7B%22id%22...``).
    Imzo (HMAC) DECODE QILINGAN qiymatlar ustida hisoblanadi, shuning uchun
    qiymatlarni albatta ``parse_qsl`` bilan decode qilish shart.
    """
    result: dict[str, str] = {}
    if not init_data:
        return result
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return result
    for key, value in pairs:
        if key:
            result[key] = value
    return result


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age: int = INITDATA_MAX_AGE,
    now: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Telegram ``initData`` ni HAQIQIY kriptografik tekshiruvdan o'tkazadi.

    Qaytaradi:
        dict  — tekshiruv muvaffaqiyatli (``user_id`` va ``auth_date`` bilan);
        None  — soxta, muddati o'tgan yoki bo'sh ``initData``.
    """
    if not init_data or not bot_token:
        return None

    params = parse_init_data(init_data)
    received_hash = params.pop("hash", "")
    if not received_hash:
        return None

    # DIQQAT: bot-token (HMAC-SHA256) usulida FAQAT ``hash`` chiqarib
    # tashlanadi. ``signature`` maydoni data-check-string ichida QOLADI —
    # uni faqat Ed25519 (uchinchi tomon) usulida chiqarib tashlash kerak.

    check_string = "\n".join(
        f"{key}={params[key]}" for key in sorted(params)
    )
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    expected = hmac.new(
        secret_key, check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None

    auth_date_raw = params.get("auth_date", "")
    if not auth_date_raw.isdigit():
        return None
    age = (now if now is not None else time.time()) - int(auth_date_raw)
    if age > max_age or age < -max_age:  # kelajakdagi (noto'g'ri) vaqt ham rad
        return None

    user_id: Optional[int] = None
    raw_user = params.get("user", "")
    if raw_user:
        try:
            user_obj = json.loads(raw_user)
            user_id = int(user_obj.get("id"))
        except (ValueError, TypeError, AttributeError):
            user_id = None
    if not user_id:
        return None

    return {
        "params": params,
        "user_id": user_id,
        "auth_date": int(auth_date_raw),
    }


def _init_data_of(request: web.Request) -> str:
    """initData'ni faqat maxsus headerdan oladi.

    URL query parametridagi credential proxy/access-log va browser historyga
    tushib qolishi mumkin, shuning uchun production API faqat headerni qabul
    qiladi.
    """
    return request.headers.get("X-Telegram-Init-Data") or ""


def authorize(
    request: web.Request,
) -> tuple[Optional[dict[str, Any]], Optional[web.Response]]:
    """SERVER-SIDE autentifikatsiya + avtorizatsiya (HTTP semantikasi bilan).

    Qaytaradi ``(identity, error_response)``:

    * initData yaroqsiz (yo'q / soxta / muddati o'tgan / imzo xato)
      -> ``(None, 401)`` — autentifikatsiya yo'q;
    * initData YAROQLI, lekin foydalanuvchi hech qanday panel rolida emas
      -> ``(None, 403)`` — autentifikatsiya bor, ruxsat yo'q;
    * ruxsat berilgan -> ``({"user_id": .., "role": ..}, None)``.
    """
    data = validate_init_data(_init_data_of(request), settings.bot_token)
    if not data:
        return None, _unauthorized()

    user_id = int(data["user_id"])
    role = admin_roles.role_of(user_id)
    if role is None:
        logger.warning(
            "Web admin panel: ruxsatsiz foydalanuvchi rad etildi (user=%s)",
            user_id,
        )
        return None, _forbidden()
    return {"user_id": user_id, "role": role}, None


def _authorize_permission(
    request: web.Request, permission: str
) -> tuple[Optional[dict[str, Any]], Optional[web.Response]]:
    """Autentifikatsiya + ANIQ ruxsat tekshiruvi (403 + sabab qaytadi)."""
    identity, error = authorize(request)
    if error is not None:
        return None, error
    if not perms.role_has(identity["role"], permission):
        logger.warning(
            "Web panel: ruxsat rad etildi (user=%s role=%s permission=%s)",
            identity["user_id"], identity["role"], permission,
        )
        return None, _forbidden_permission(permission)
    return identity, None


def authenticate(request: web.Request) -> Optional[dict[str, Any]]:
    """Qulaylik ko'rinishi: ruxsat berilgan bo'lsa identity, aks holda ``None``."""
    identity, _ = authorize(request)
    return identity


def check_auth(request: web.Request) -> bool:
    """Qulaylik uchun boolean ko'rinish (True — autentifikatsiya o'tdi)."""
    return authenticate(request) is not None


def _unauthorized() -> web.Response:
    return web.json_response({"error": "Unauthorized"}, status=401, headers=_NO_CACHE)


def _forbidden() -> web.Response:
    """Yaroqli initData, lekin panel roli yo'q — 403 (401 emas)."""
    return web.json_response({"error": "Forbidden"}, status=403, headers=_NO_CACHE)


def _forbidden_permission(permission: str) -> web.Response:
    return web.json_response(
        {"error": "Forbidden", "permission": permission},
        status=403,
        headers=_NO_CACHE,
    )


def _is_owner(identity: dict[str, Any]) -> bool:
    return identity.get("role") == perms.ROLE_OWNER


async def _read_json(request: web.Request) -> tuple[Optional[dict[str, Any]], Optional[web.Response]]:
    """JSON tanani o'qiydi (xato -> 400 javobi)."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return None, web.json_response({"error": "Invalid JSON"}, status=400)
    if not isinstance(data, dict):
        return None, web.json_response({"error": "Invalid payload"}, status=400)
    return data, None


def _positive_int(value: Any) -> Optional[int]:
    """Strict positive integer parser for Telegram IDs.

    ``int(123.9)`` silently becomes ``123`` in Python, which is dangerous for
    admin/moderation actions. Only real ints or digit-only strings are accepted.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            parsed = int(raw)
            return parsed if parsed > 0 else None
    return None


async def _notify_role_user(
    request: web.Request,
    user_id: int,
    text: str,
    *,
    op: str,
) -> bool:
    """Best-effort role notification. Role change itself remains authoritative.

    Telegram may reject the send when the target has never started the bot or
    has blocked it. In that case the admin operation is not rolled back; the
    API explicitly returns ``notification_sent=false`` so the owner knows.
    """
    bot = request.app.get("bot")
    if bot is None:
        return False
    try:
        await tg_call(
            op,
            lambda: bot.send_message(int(user_id), text, parse_mode="HTML"),
            attempts=2,
        )
        return True
    except Exception:  # noqa: BLE001
        logger.info("Role notification failed (user=%s op=%s)", user_id, op, exc_info=True)
        return False


async def _sync_admin_menu_button(
    request: web.Request, user_id: int, *, enabled: bool
) -> bool:
    """Runtime admin qo'shilishi/o'chirilishi bilan Telegram menu tugmasini sync qiladi.

    Startup paytida main.py barcha env/runtime adminlarga tugmani o'rnatadi, lekin
    paneldan yangi admin qo'shilganda restart kutmasligi kerak. O'chirilganda esa
    eski "Admin panel" tugmasi userda qolib ketmasligi uchun defaultga qaytaramiz.
    """
    bot = request.app.get("bot")
    if bot is None:
        return False
    try:
        from aiogram.types import MenuButtonDefault, MenuButtonWebApp, WebAppInfo
        from app.keyboards.user_kb import webapp_url

        menu_button = (
            MenuButtonWebApp(text="Admin panel", web_app=WebAppInfo(url=webapp_url()))
            if enabled
            else MenuButtonDefault()
        )
        await tg_call(
            "admin_menu_button_sync",
            lambda: bot.set_chat_menu_button(chat_id=int(user_id), menu_button=menu_button),
            attempts=2,
        )
        return True
    except Exception:  # noqa: BLE001
        logger.info(
            "Admin menu button sync failed (user=%s enabled=%s)",
            user_id, enabled, exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# Broadcast (moslik uchun eski nomlar)
# ---------------------------------------------------------------------------
_broadcast_state = broadcast_engine.state
_broadcast_task: Optional[asyncio.Task] = None
_BROADCAST_STATE_KEY = broadcast_engine.BROADCAST_STATE_KEY
_BROADCAST_STATE_VERSION = broadcast_engine.BROADCAST_STATE_VERSION
broadcast_snapshot = broadcast_engine.snapshot
recover_broadcast_state = broadcast_engine.recover_state


# ---------------------------------------------------------------------------
# API endpointlari
# ---------------------------------------------------------------------------
async def api_me(request: web.Request) -> web.Response:
    """Joriy panel foydalanuvchisining server-side identity/ruxsatlari.

    Frontend bootstrap uchun analytics kabi og'ir endpointga bog'lanmaslik
    kerak: authentication ishlasa, bu endpoint minimal va barqaror javob beradi.
    """
    identity, error = authorize(request)
    if error is not None:
        return error
    assert identity is not None
    return web.json_response(
        {
            "user_id": identity["user_id"],
            "role": identity["role"],
            "is_owner": _is_owner(identity),
            "permissions": sorted(perms.permissions_for(identity["role"])),
        },
        headers=_NO_CACHE,
    )


async def api_stats(request: web.Request) -> web.Response:
    """Dashboard: faqat yengil aggregate hisoblar (ruxsat: dashboard.view)."""
    identity, error = _authorize_permission(request, perms.P_DASHBOARD_VIEW)
    if error is not None:
        return error

    try:
        total_users = await db.count_users()
        active_conns = await db.count_connections(enabled_only=True)
        deleted_msgs = (
            await db.count_events(event_type="delete")
            + await db.count_events(event_type="delete_media")
        )
        edited_msgs = await db.count_events(event_type="edit")

        return web.json_response(
            {
                "role": identity["role"],
                "is_owner": _is_owner(identity),
                "permissions": sorted(perms.permissions_for(identity["role"])),
                "total_users": total_users,
                "active_connections": active_conns,
                "deleted_messages": deleted_msgs,
                "edited_messages": edited_msgs,
            },
            headers=_NO_CACHE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("API Stats Error: %s", exc, exc_info=True)
        return web.json_response({"error": "Server error"}, status=500)


async def api_analytics(request: web.Request) -> web.Response:
    """Analytics: ko'rsatkichlar + vaqt kesimi (ruxsat: analytics.view).

    Query: ``?range=24h|7d|30d|custom&start=YYYY-MM-DD&end=YYYY-MM-DD``.
    Barcha raqamlar BAZADAN (hardcode/demo yo'q).
    """
    identity, error = _authorize_permission(request, perms.P_ANALYTICS_VIEW)
    if error is not None:
        return error

    range_name = request.query.get("range") or analytics.DEFAULT_RANGE
    try:
        payload = await analytics.dashboard(
            range_name,
            request.query.get("start"),
            request.query.get("end"),
        )
    except analytics.AnalyticsError as exc:
        return web.json_response({"error": str(exc)}, status=400, headers=_NO_CACHE)
    except Exception as exc:  # noqa: BLE001
        logger.error("API Analytics error: %s", exc, exc_info=True)
        return web.json_response({"error": "Server error"}, status=500)
    payload["role"] = identity["role"]
    payload["permissions"] = sorted(perms.permissions_for(identity["role"]))
    return web.json_response(payload, headers=_NO_CACHE)


async def api_get_settings(request: web.Request) -> web.Response:
    identity, error = _authorize_permission(request, perms.P_SETTINGS_VIEW)
    if error is not None:
        return error
    try:
        hold_mode = await maintenance.is_enabled()
        raw_retention = await db.get_setting("retention_days", "90")
        retention = int(raw_retention) if raw_retention.isdigit() else 90
        return web.json_response(
            {
                "role": identity["role"],
                "is_owner": _is_owner(identity),
                "can_write": perms.role_has(identity["role"], perms.P_SETTINGS_WRITE),
                "permissions": sorted(perms.permissions_for(identity["role"])),
                "maintenance_mode": hold_mode,
                "resume_notify": await maintenance.resume_notification_enabled(),
                "retention_days": retention,
            },
            headers=_NO_CACHE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("API Settings read error: %s", exc, exc_info=True)
        return web.json_response({"error": "Server error"}, status=500)


async def api_set_settings(request: web.Request) -> web.Response:
    identity, error = _authorize_permission(request, perms.P_SETTINGS_WRITE)
    if error is not None:
        return error

    data, error = await _read_json(request)
    if error is not None:
        return error
    assert data is not None

    # Avval BUTUN payloadni tekshiramiz, keyin side-effect qilamiz. Aks holda
    # bitta requestda maintenance to'g'ri, retention noto'g'ri bo'lsa Hold
    # o'zgarib ketib, API 400 qaytaradigan partial-update holati yuz berardi.
    if "maintenance_mode" in data and not isinstance(data["maintenance_mode"], bool):
        return web.json_response(
            {"error": "maintenance_mode must be a boolean"},
            status=400, headers=_NO_CACHE,
        )
    raw_retention = str(data.get("retention_days", "")).strip()
    if raw_retention and (
        not raw_retention.isdigit() or int(raw_retention) not in (7, 30, 90, 180)
    ):
        return web.json_response(
            {"error": "retention_days must be one of 7/30/90/180"},
            status=400, headers=_NO_CACHE,
        )

    result: dict[str, Any] = {}
    try:
        if "maintenance_mode" in data:
            enabled = data["maintenance_mode"]
            # Telegram admin panel bilan AYNAN bir xil persistent holat.
            notify_result = await maintenance.set_enabled(enabled, notify_resume=True)
            await audit.log_action(
                "maintenance_mode",
                actor=identity["user_id"],
                actor_role=identity["role"],
                target="maintenance_mode",
                result="ok",
                new="on" if enabled else "off",
                severity="WARNING",
            )
            result["maintenance_mode"] = enabled
            result["maintenance_notifications"] = notify_result or {}
        if "resume_notify" in data:
            # Legacy field is intentionally ignored: maintenance notifications
            # are now mandatory on every real ON/OFF transition.
            result["resume_notify"] = True
        if raw_retention:
            previous = await db.get_setting("retention_days", "90")
            await db.set_setting("retention_days", raw_retention)
            await audit.log_action(
                "retention_changed",
                actor=identity["user_id"],
                actor_role=identity["role"],
                target="retention_days",
                old=previous,
                new=raw_retention,
                severity="WARNING",
            )
            result["retention_days"] = int(raw_retention)
        return web.json_response({"ok": True, "settings": result}, headers=_NO_CACHE)
    except Exception as exc:  # noqa: BLE001
        logger.error("API Settings write error: %s", exc, exc_info=True)
        return web.json_response({"error": "Server error"}, status=500)


# ---------------------------------------------------------------------------
# AD BUILDER: preview / test send
# ---------------------------------------------------------------------------
async def api_broadcast_preview(request: web.Request) -> web.Response:
    """Reklama preview'i — yuborilmasdan oldin AYNAN tuzilishni ko'rsatadi."""
    identity, error = _authorize_permission(request, perms.P_BROADCAST_USE)
    if error is not None:
        return error
    data, error = await _read_json(request)
    if error is not None:
        return error
    assert data is not None
    try:
        ad = broadcast_engine.build_ad(data)
    except AdError as exc:
        return web.json_response({"error": str(exc)}, status=400, headers=_NO_CACHE)
    payload = broadcast_engine.preview(ad)
    payload["recipients"] = await broadcast_engine.count_recipients()
    return web.json_response(payload, headers=_NO_CACHE)


async def api_broadcast_test(request: web.Request) -> web.Response:
    """TEST yuborish — FAQAT bitta oluvchiga (broadcast boshlanmaydi)."""
    identity, error = _authorize_permission(request, perms.P_BROADCAST_USE)
    if error is not None:
        return error
    data, error = await _read_json(request)
    if error is not None:
        return error
    assert data is not None
    try:
        ad = broadcast_engine.build_ad(data)
    except AdError as exc:
        return web.json_response({"error": str(exc)}, status=400, headers=_NO_CACHE)

    target = data.get("test_recipient")
    if target in (None, ""):
        target_id = int(identity["user_id"])
    else:
        target_id = _positive_int(target)
        if target_id is None:
            return web.json_response({"error": "test_recipient must be a positive integer"}, status=400)

    bot = request.app.get("bot")
    if bot is None:
        return web.json_response({"error": "Bot instance not available"}, status=503)
    try:
        await broadcast_engine.test_send(bot, ad, target_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Test yuborish xatosi (target=%s): %s", target_id, exc)
        return web.json_response(
            {"error": f"Test yuborilmadi: {type(exc).__name__}"}, status=502
        )
    await audit.log_action(
        "broadcast_test_sent",
        actor=identity["user_id"],
        actor_role=identity["role"],
        target=target_id,
        details="test yuborish (broadcast boshlanmadi)",
    )
    # MUHIM: test yuborish broadcast statistikasiga TA'SIR QILMAYDI.
    return web.json_response(
        {"ok": True, "target": target_id, "status": broadcast_snapshot()},
        headers=_NO_CACHE,
    )


async def api_broadcast(request: web.Request) -> web.Response:
    """Broadcastni boshlaydi (fon vazifasi) — 202 qabul qilindi / 409 band."""
    identity, error = _authorize_permission(request, perms.P_BROADCAST_USE)
    if error is not None:
        return error
    data, error = await _read_json(request)
    if error is not None:
        return error
    assert data is not None
    try:
        ad = broadcast_engine.build_ad(data)
    except AdError as exc:
        return web.json_response({"error": str(exc)}, status=400, headers=_NO_CACHE)

    bot = request.app.get("bot")
    if bot is None:
        return web.json_response({"error": "Bot instance not available"}, status=503)

    # Hold mode: YANGI broadcast boshlanmaydi (ishlab turgani TO'XTATILMAYDI).
    if await maintenance.is_enabled():
        return web.json_response(
            {"error": "Hold mode active — broadcast cannot start", "hold_mode": True},
            status=503,
            headers=_NO_CACHE,
        )

    # Takroriy bosish: bir vaqtda faqat bitta broadcast ishlaydi.
    if broadcast_engine.is_running():
        return web.json_response(
            {"error": "Broadcast already running", "status": broadcast_snapshot()},
            status=409,
        )

    try:
        result = await broadcast_engine.start(
            bot, ad, requested_by=identity["user_id"]
        )
    except BroadcastBusy:
        return web.json_response(
            {"error": "Broadcast already running", "status": broadcast_snapshot()},
            status=409,
        )
    except AdError as exc:
        return web.json_response({"error": str(exc)}, status=400, headers=_NO_CACHE)
    except Exception as exc:  # noqa: BLE001
        logger.error("Broadcast start error: %s", exc, exc_info=True)
        return web.json_response({"error": "Server error"}, status=500)

    await audit.log_action(
        "broadcast_started",
        actor=identity["user_id"],
        actor_role=identity["role"],
        target=result["broadcast_id"],
        new=f"total={result['total']} buttons={len(ad.buttons)}",
        severity="WARNING",
    )
    return web.json_response(
        {
            "accepted": True,
            "total": result["total"],
            "broadcast_id": result["broadcast_id"],
            "status": broadcast_snapshot(),
        },
        status=202,
        headers=_NO_CACHE,
    )


async def api_broadcast_retry(request: web.Request) -> web.Response:
    """FAQAT xato olgan oluvchilarga qayta yuboradi (Retry Failed)."""
    identity, error = _authorize_permission(request, perms.P_BROADCAST_USE)
    if error is not None:
        return error
    bot = request.app.get("bot")
    if bot is None:
        return web.json_response({"error": "Bot instance not available"}, status=503)
    if await maintenance.is_enabled():
        return web.json_response(
            {"error": "Hold mode active — broadcast cannot start", "hold_mode": True},
            status=503,
            headers=_NO_CACHE,
        )
    try:
        result = await broadcast_engine.retry_failed(
            bot, requested_by=identity["user_id"]
        )
    except BroadcastBusy:
        return web.json_response(
            {"error": "Broadcast already running", "status": broadcast_snapshot()},
            status=409,
        )
    except AdError as exc:
        return web.json_response({"error": str(exc)}, status=400, headers=_NO_CACHE)
    except Exception as exc:  # noqa: BLE001
        logger.error("Broadcast retry error: %s", exc, exc_info=True)
        return web.json_response({"error": "Server error"}, status=500)

    await audit.log_action(
        "broadcast_retry_failed",
        actor=identity["user_id"],
        actor_role=identity["role"],
        target=result["broadcast_id"],
        new=f"retried={result['total']}",
        severity="WARNING",
    )
    return web.json_response(
        {
            "accepted": True,
            "total": result["total"],
            "broadcast_id": result["broadcast_id"],
            "status": broadcast_snapshot(),
        },
        status=202,
        headers=_NO_CACHE,
    )


async def api_broadcast_status(request: web.Request) -> web.Response:
    """Broadcast holati: running/total/sent/failed/pending + xato olganlar."""
    identity, error = _authorize_permission(request, perms.P_BROADCAST_VIEW)
    if error is not None:
        return error
    try:
        return web.json_response(
            await broadcast_engine.status_payload(), headers=_NO_CACHE
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Broadcast status error: %s", exc, exc_info=True)
        return web.json_response({"error": "Server error"}, status=500)


# ---------------------------------------------------------------------------
# USERS / CONNECTION RECONCILIATION
# ---------------------------------------------------------------------------
def _user_payload(row: dict) -> dict:
    uid = int(row.get("user_id") or 0)
    return {
        "user_id": uid,
        "username": row.get("username") or "",
        "first_name": row.get("first_name") or "",
        "last_name": row.get("last_name") or "",
        "created_at": row.get("created_at"),
        "last_activity": row.get("last_activity"),
        "connected_at": row.get("connected_at"),
        "connections_count": int(row.get("connections_count") or 0),
        "active_connections": int(row.get("active_connections") or 0),
        "connected": bool(int(row.get("active_connections") or 0) > 0),
        "banned": moderation.is_banned(uid),
        "role": admin_roles.role_of(uid),
        "protected": admin_roles.is_admin(uid),
    }


async def api_users(request: web.Request) -> web.Response:
    """Server-side user directory with pagination/search."""
    identity, error = _authorize_permission(request, perms.P_USERS_VIEW)
    if error is not None:
        return error
    search = request.query.get("search", "").strip()[:120]
    try:
        limit = max(1, min(int(request.query.get("limit") or 50), 100))
        offset = max(0, int(request.query.get("offset") or 0))
    except ValueError:
        return web.json_response({"error": "Invalid pagination"}, status=400)
    try:
        rows = await db.users_page(search, limit=limit, offset=offset)
        total = await db.count_users_filtered(search)
        return web.json_response(
            {
                "role": identity["role"],
                "permissions": sorted(perms.permissions_for(identity["role"])),
                "items": [_user_payload(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_next": offset + len(rows) < total,
            },
            headers=_NO_CACHE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("API Users error: %s", exc, exc_info=True)
        return web.json_response({"error": "Server error"}, status=500)


async def api_user_detail(request: web.Request) -> web.Response:
    identity, error = _authorize_permission(request, perms.P_USERS_VIEW)
    if error is not None:
        return error
    user_id = _positive_int(request.match_info.get("user_id"))
    if user_id is None:
        return web.json_response({"error": "Invalid user_id"}, status=400)
    try:
        row = await db.get_user(user_id)
        if not row:
            return web.json_response({"error": "User not found"}, status=404)
        stats = await db.user_stats(user_id)
        payload = _user_payload(row)
        payload["stats"] = stats
        payload["connections"] = await db.connections_for_user(user_id)
        payload["events"] = [
            _event_payload(row)
            for row in await db.search_events(user_id=user_id, limit=12, offset=0)
        ]
        payload["requested_by_role"] = identity["role"]
        return web.json_response(payload, headers=_NO_CACHE)
    except Exception as exc:  # noqa: BLE001
        logger.error("API User detail error: %s", exc, exc_info=True)
        return web.json_response({"error": "Server error"}, status=500)


def _event_payload(row: dict) -> dict[str, Any]:
    """Admin UI uchun xavfsiz va ixcham event JSON."""
    return {
        "id": int(row.get("id") or 0),
        "user_id": int(row.get("user_id") or 0),
        "chat_id": row.get("chat_id"),
        "chat_title": row.get("chat_title") or "",
        "event_type": row.get("event_type") or "",
        "message_id": row.get("message_id"),
        "details": str(row.get("details") or "")[:4000],
        "sender_id": row.get("sender_id"),
        "business_connection_id": row.get("business_connection_id") or "",
        "occurred_at": row.get("occurred_at"),
    }


async def api_send_user_message(request: web.Request) -> web.Response:
    """Owner/admin panelidan bitta foydalanuvchiga oddiy Telegram xabari."""
    identity, error = _authorize_permission(request, perms.P_USERS_MESSAGE)
    if error is not None:
        return error
    user_id = _positive_int(request.match_info.get("user_id"))
    if user_id is None:
        return web.json_response({"error": "Invalid user_id"}, status=400, headers=_NO_CACHE)
    data, error = await _read_json(request)
    if error is not None:
        return error
    assert data is not None
    text = str(data.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "Message text is required"}, status=400, headers=_NO_CACHE)
    if len(text) > 4096:
        return web.json_response({"error": "Message is too long (max 4096)"}, status=400, headers=_NO_CACHE)
    bot = request.app.get("bot")
    if bot is None:
        return web.json_response({"error": "Bot instance not available"}, status=503, headers=_NO_CACHE)
    try:
        await tg_call(
            "admin_direct_message",
            lambda: bot.send_message(user_id, text),
            attempts=3,
        )
        await audit.log_action(
            "admin_direct_message",
            actor=identity["user_id"],
            actor_role=identity["role"],
            target=user_id,
            result="ok",
            details=f"length={len(text)}",
        )
        return web.json_response({"ok": True, "user_id": user_id}, headers=_NO_CACHE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Admin direct message failed (user=%s): %s", user_id, exc)
        return web.json_response(
            {"error": f"Message was not delivered: {type(exc).__name__}"},
            status=502, headers=_NO_CACHE,
        )


async def api_delete_user(request: web.Request) -> web.Response:
    """Operational user data-ni DB'dan butunlay o'chirish."""
    identity, error = _authorize_permission(request, perms.P_USERS_DELETE)
    if error is not None:
        return error
    user_id = _positive_int(request.match_info.get("user_id"))
    if user_id is None:
        return web.json_response({"error": "Invalid user_id"}, status=400)
    if _is_protected_user(user_id):
        return web.json_response({"error": "Admin/owner cannot be deleted"}, status=403, headers=_NO_CACHE)
    try:
        changed = await db.delete_user(user_id)
        if not changed:
            return web.json_response({"error": "User not found"}, status=404, headers=_NO_CACHE)
        cleanup_warnings: list[str] = []
        try:
            await moderation.remove_ban_without_notification(user_id)
        except Exception:  # noqa: BLE001
            # Asosiy DB tranzaksiyasi allaqachon muvaffaqiyatli tugagan. Ban
            # snapshot cleanup xatosi sabab clientga noto'g'ri "delete failed"
            # demaymiz; log + warning qaytaramiz.
            logger.error("Ban cleanup after user delete failed (user=%s)", user_id, exc_info=True)
            cleanup_warnings.append("ban_cleanup_failed")
        try:
            from app.services import reporter
            reporter.clear_connection_states()
            reporter.invalidate_user(user_id)
        except Exception:  # noqa: BLE001
            logger.debug("Reporter cache cleanup after user delete failed", exc_info=True)
            cleanup_warnings.append("reporter_cache_cleanup_failed")
        await audit.log_action(
            "user_deleted",
            actor=identity["user_id"],
            actor_role=identity["role"],
            target=user_id,
            result="ok",
            severity="WARNING",
        )
        return web.json_response(
            {
                "ok": True,
                "deleted": True,
                "user_id": user_id,
                "cleanup_warnings": cleanup_warnings,
            },
            headers=_NO_CACHE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("API User delete error: %s", exc, exc_info=True)
        return web.json_response({"error": "Server error"}, status=500)



# ---------------------------------------------------------------------------
# ADMIN MANAGEMENT (OWNER ONLY via permissions matrix)
# ---------------------------------------------------------------------------
def _admin_payload(user_id: int, role: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "user_id": int(user_id),
        "role": role,
        "is_owner": role == perms.ROLE_OWNER,
        "runtime_managed": admin_roles.has_runtime(int(user_id)),
        "removable": admin_roles.has_runtime(int(user_id)),
        "role_editable": admin_roles.has_runtime(int(user_id)),
    }
    return payload


async def api_admins(request: web.Request) -> web.Response:
    identity, error = _authorize_permission(request, perms.P_ADMINS_VIEW)
    if error is not None:
        return error
    rows = []
    for user_id, role in sorted(admin_roles.list_admins().items()):
        row = _admin_payload(user_id, role)
        try:
            user = await db.get_user(user_id)
        except Exception:
            user = None
        if user:
            row.update({
                "username": user.get("username"),
                "first_name": user.get("first_name"),
                "last_name": user.get("last_name"),
            })
        rows.append(row)
    return web.json_response(
        {
            "admins": rows,
            "count": len(rows),
            "role": identity["role"],
            "can_manage": perms.role_has(identity["role"], perms.P_ADMINS_MANAGE),
        },
        headers=_NO_CACHE,
    )


async def api_add_admin(request: web.Request) -> web.Response:
    identity, error = _authorize_permission(request, perms.P_ADMINS_MANAGE)
    if error is not None:
        return error
    data, error = await _read_json(request)
    if error is not None:
        return error
    assert data is not None
    user_id = _positive_int(data.get("user_id"))
    if user_id is None:
        return web.json_response({"error": "user_id must be a positive integer"}, status=400)
    role = perms.normalize_role(data.get("role") or perms.ROLE_ADMIN)
    if role not in perms.MANAGED_ROLES:
        return web.json_response(
            {"error": "role must be ADMIN, MODERATOR or VIEWER"}, status=400
        )
    if admin_roles.role_of(user_id) is not None:
        return web.json_response({"error": "User already has an admin role"}, status=409)
    try:
        changed = await admin_roles.add(user_id, role)
        if not changed:
            return web.json_response({"error": "Admin could not be added"}, status=409)

        # Bir user admin/moderator/viewer bo'lsa middleware baribir uni bandan
        # mustasno qiladi. Lekin Moderation ro'yxatida "BANNED admin" qolib
        # ketishi chalkash va keyin roli olib tashlansa kutilmaganda bloklanadi.
        # Shuning uchun promotion paytida eski ban yozuvini jimgina tozalaymiz.
        cleanup_warnings: list[str] = []
        if moderation.is_banned(user_id):
            try:
                cleaned = await moderation.remove_ban_without_notification(user_id)
                if not cleaned and moderation.is_banned(user_id):
                    cleanup_warnings.append("ban_cleanup_failed")
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Admin promotion paytida eski ban yozuvi tozalanmadi (user=%s)",
                    user_id, exc_info=True,
                )
                cleanup_warnings.append("ban_cleanup_failed")

        await audit.log_action(
            "admin_added",
            actor=identity["user_id"],
            actor_role=identity["role"],
            target=user_id,
            result="ok",
            new=role,
            severity="WARNING",
        )
        from app.utils import texts
        notification_sent = await _notify_role_user(
            request, user_id, texts.ADMIN_ROLE_ASSIGNED.format(role=role.upper()),
            op="admin_role_assigned_notify",
        )
        menu_button_synced = await _sync_admin_menu_button(request, user_id, enabled=True)
        return web.json_response(
            {"ok": True, "changed": True, "notification_sent": notification_sent,
             "menu_button_synced": menu_button_synced,
             "cleanup_warnings": cleanup_warnings,
             **_admin_payload(user_id, role)},
            status=201, headers=_NO_CACHE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Admin add error: %s", exc, exc_info=True)
        return web.json_response({"error": "Server error"}, status=500)


async def api_set_admin_role(request: web.Request) -> web.Response:
    identity, error = _authorize_permission(request, perms.P_ADMINS_MANAGE)
    if error is not None:
        return error
    user_id = _positive_int(request.match_info.get("user_id"))
    if user_id is None:
        return web.json_response({"error": "Invalid user_id"}, status=400)
    data, error = await _read_json(request)
    if error is not None:
        return error
    assert data is not None
    role = perms.normalize_role(data.get("role"))
    if role not in perms.MANAGED_ROLES:
        return web.json_response(
            {"error": "role must be ADMIN, MODERATOR or VIEWER"}, status=400
        )
    current = admin_roles.role_of(user_id)
    if current is None:
        return web.json_response({"error": "Admin not found"}, status=404)
    if current == perms.ROLE_OWNER or not admin_roles.has_runtime(user_id):
        return web.json_response(
            {"error": "OWNER/env-managed admin role cannot be changed here"}, status=403
        )
    try:
        if current == role:
            return web.json_response(
                {"ok": True, "changed": False, "notification_sent": False,
                 **_admin_payload(user_id, role)},
                headers=_NO_CACHE,
            )
        changed = await admin_roles.set_role(user_id, role)
        if not changed:
            return web.json_response({"error": "Role was not changed"}, status=409)
        await audit.log_action(
            "admin_role_changed",
            actor=identity["user_id"],
            actor_role=identity["role"],
            target=user_id,
            result="ok",
            old=current,
            new=role,
            severity="WARNING",
        )
        from app.utils import texts
        notification_sent = await _notify_role_user(
            request, user_id,
            texts.ADMIN_ROLE_CHANGED.format(
                old_role=str(current).upper(), new_role=role.upper()
            ),
            op="admin_role_changed_notify",
        )
        return web.json_response(
            {"ok": True, "changed": True, "notification_sent": notification_sent,
             **_admin_payload(user_id, role)},
            headers=_NO_CACHE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Admin role change error: %s", exc, exc_info=True)
        return web.json_response({"error": "Server error"}, status=500)


async def api_delete_admin(request: web.Request) -> web.Response:
    identity, error = _authorize_permission(request, perms.P_ADMINS_MANAGE)
    if error is not None:
        return error
    user_id = _positive_int(request.match_info.get("user_id"))
    if user_id is None:
        return web.json_response({"error": "Invalid user_id"}, status=400)
    current = admin_roles.role_of(user_id)
    if current is None:
        return web.json_response({"error": "Admin not found"}, status=404)
    if current == perms.ROLE_OWNER or not admin_roles.has_runtime(user_id):
        return web.json_response(
            {"error": "OWNER/env-managed admin cannot be removed here"}, status=403
        )
    try:
        changed = await admin_roles.remove(user_id)
        if not changed:
            return web.json_response({"error": "Admin could not be removed"}, status=409)
        await audit.log_action(
            "admin_removed",
            actor=identity["user_id"],
            actor_role=identity["role"],
            target=user_id,
            result="ok",
            old=current,
            severity="WARNING",
        )
        from app.utils import texts
        notification_sent = await _notify_role_user(
            request, user_id,
            texts.ADMIN_ROLE_REMOVED.format(old_role=str(current).upper()),
            op="admin_role_removed_notify",
        )
        menu_button_synced = await _sync_admin_menu_button(request, user_id, enabled=False)
        return web.json_response(
            {"ok": True, "user_id": user_id, "removed": True,
             "notification_sent": notification_sent, "menu_button_synced": menu_button_synced},
            headers=_NO_CACHE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Admin remove error: %s", exc, exc_info=True)
        return web.json_response({"error": "Server error"}, status=500)


async def api_verify_connections(request: web.Request) -> web.Response:
    identity, error = _authorize_permission(request, perms.P_CONNECTIONS_MANAGE)
    if error is not None:
        return error
    try:
        result = await connection_verify.verify_all(force=False, notify=True)
        await audit.log_action(
            "connections_verify",
            actor=identity["user_id"],
            actor_role=identity["role"],
            target="all",
            result="ok",
            new=str(result),
        )
        return web.json_response(result, headers=_NO_CACHE)
    except Exception as exc:  # noqa: BLE001
        logger.error("Connection verification error: %s", exc, exc_info=True)
        return web.json_response({"error": "Server error"}, status=500)


# ---------------------------------------------------------------------------
# MODERATSIYA (ban / unban) — ruxsat: users.view / users.moderate
# ---------------------------------------------------------------------------
def _is_protected_user(user_id: int) -> bool:
    """Admin/owner hech qachon ban qilinmaydi (server-side himoya)."""
    return admin_roles.is_admin(user_id)


async def api_moderation(request: web.Request) -> web.Response:
    """Ban ro'yxati."""
    identity, error = _authorize_permission(request, perms.P_USERS_VIEW)
    if error is not None:
        return error
    try:
        limit = int(request.query.get("limit") or 200)
    except ValueError:
        limit = 200
    rows = moderation.list_banned(limit=max(1, min(limit, 1000)))
    return web.json_response(
        {
            "count": moderation.count_banned(),
            "banned": rows,
            "role": identity["role"],
        },
        headers=_NO_CACHE,
    )


async def api_moderate_user(request: web.Request) -> web.Response:
    """Ban yoki unban (``{"user_id": 123, "reason": "...", "action": "ban"}``)."""
    identity, error = _authorize_permission(request, perms.P_USERS_MODERATE)
    if error is not None:
        return error
    data, error = await _read_json(request)
    if error is not None:
        return error
    assert data is not None
    user_id = _positive_int(data.get("user_id"))
    if user_id is None:
        return web.json_response({"error": "user_id must be a positive integer"}, status=400)
    action = str(data.get("action") or "ban").strip().lower()
    if action not in ("ban", "unban"):
        return web.json_response({"error": "action must be ban|unban"}, status=400)
    if action == "ban" and _is_protected_user(user_id):
        return web.json_response(
            {"error": "Admin/owner cannot be banned"}, status=403, headers=_NO_CACHE
        )

    try:
        if action == "ban":
            if moderation.is_banned(user_id):
                changed = False
                event, result_text = "user_banned", "already banned"
            else:
                changed = await moderation.ban(
                    user_id, by=identity["user_id"], reason=str(data.get("reason") or "")
                )
                if not changed:
                    # Bu yerga kelganda user avval ban emas edi. Demak False
                    # "already banned" emas, persist/DB xatosi bo'lishi mumkin.
                    raise RuntimeError("Ban state could not be persisted")
                event, result_text = "user_banned", "banned"
        else:
            if not moderation.is_banned(user_id):
                changed = False
                event, result_text = "user_unbanned", "not banned"
            else:
                changed = await moderation.unban(user_id)
                if not changed:
                    raise RuntimeError("Unban state could not be persisted")
                event, result_text = "user_unbanned", "unbanned"

        await audit.log_action(
            event,
            actor=identity["user_id"],
            actor_role=identity["role"],
            target=user_id,
            result="ok",
            new=result_text,
            severity="WARNING",
        )
        return web.json_response(
            {
                "ok": True,
                "changed": bool(changed),
                "user_id": user_id,
                "banned": moderation.is_banned(user_id),
                "count": moderation.count_banned(),
            },
            headers=_NO_CACHE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Moderation action failed (action=%s user=%s): %s", action, user_id, exc, exc_info=True)
        return web.json_response({"error": "Moderation state could not be saved"}, status=500, headers=_NO_CACHE)


# ---------------------------------------------------------------------------
# HEALTHCHECK / READINESS (Railway deployment)
# ---------------------------------------------------------------------------
_runtime: dict[str, Any] = {"ready": False, "started_at": time.time()}


def mark_ready() -> None:
    """Initializatsiya MUVAFFAQIYATLI tugadi — healthcheck 200 bera boshlaydi."""
    _runtime["ready"] = True
    logger.info("APPLICATION READY — healthcheck 200 qaytaradi")


def mark_not_ready() -> None:
    """To'xtatilish / xato holatida — endi sog'lom emasmiz."""
    _runtime["ready"] = False


def is_ready() -> bool:
    """Ilova tayyormi? (testlar va diagnostika uchun)."""
    return bool(_runtime["ready"])


async def health(request: web.Request) -> web.Response:  # noqa: ARG001
    """Railway healthcheck: auth YO'Q, sir YO'Q, destruktiv amal YO'Q."""
    if not _runtime["ready"]:
        return web.json_response({"status": "starting"}, status=503, headers=_NO_CACHE)
    return web.json_response({"status": "ok"}, headers=_NO_CACHE)


async def readiness(request: web.Request) -> web.Response:  # noqa: ARG001
    """Readiness: init tugaganmi VA baza javob beryaptimi?"""
    if not _runtime["ready"]:
        return web.json_response({"status": "starting"}, status=503, headers=_NO_CACHE)
    try:
        alive = await db.ping()
    except Exception:  # noqa: BLE001 – ping o'zi ham xato berishi mumkin
        alive = False
    if not alive:
        logger.warning("HEALTHCHECK: readiness FAILED — database javob bermadi")
        return web.json_response({"status": "degraded"}, status=503, headers=_NO_CACHE)
    return web.json_response({"status": "ready"}, headers=_NO_CACHE)


# ---------------------------------------------------------------------------
# Serverni ishga tushirish
# ---------------------------------------------------------------------------
async def start_web_server(bot=None):  # noqa: ANN201
    app = web.Application()
    app["bot"] = bot

    # Healthcheck/readiness — eng birinchi, autentifikatsiyasiz.
    app.router.add_get("/health", health)
    app.router.add_get("/ready", readiness)

    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/analytics", api_analytics)
    app.router.add_get("/api/settings", api_get_settings)
    app.router.add_post("/api/settings", api_set_settings)
    app.router.add_get("/api/broadcast/status", api_broadcast_status)
    app.router.add_post("/api/broadcast/preview", api_broadcast_preview)
    app.router.add_post("/api/broadcast/test", api_broadcast_test)
    app.router.add_post("/api/broadcast/retry", api_broadcast_retry)
    app.router.add_post("/api/broadcast", api_broadcast)
    app.router.add_get("/api/moderation", api_moderation)
    app.router.add_post("/api/moderation", api_moderate_user)
    app.router.add_get("/api/users", api_users)
    app.router.add_get("/api/users/{user_id}", api_user_detail)
    app.router.add_post("/api/users/{user_id}/message", api_send_user_message)
    app.router.add_delete("/api/users/{user_id}", api_delete_user)
    app.router.add_post("/api/connections/verify", api_verify_connections)
    app.router.add_get("/api/admins", api_admins)
    app.router.add_post("/api/admins", api_add_admin)
    app.router.add_patch("/api/admins/{user_id}", api_set_admin_role)
    app.router.add_delete("/api/admins/{user_id}", api_delete_admin)

    public_dir = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "public"
    )

    async def index_handler(request: web.Request) -> web.Response:
        return web.FileResponse(os.path.join(public_dir, "index.html"))

    app.router.add_get("/", index_handler)
    app.router.add_static("/", path=public_dir, name="public", show_index=False)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Web server successfully started at port %s", port)
    return site
