"""
Markazlashtirilgan RBAC — rollar va ruxsatlar matritsasi (YAGONA MANBA).

NEGA KERAK
----------
Rollar bo'yicha tekshiruvlar handlerlarga SOCHILIB ketmasligi uchun barcha
ruxsatlar BITTA joyda e'lon qilinadi va FAQAT shu yerdan o'qiladi.

ROLLAR (yuqoridan pastga — kuch kamayadi)
-----------------------------------------
* ``owner``     — to'liq huquq: admin boshqaruvi, tizim sozlamalari, baza,
                  broadcast, analytics, moderatsiya, destruktiv amallar.
* ``admin``     — deyarli barcha operatsion amallar (foydalanuvchi/ulanish/
                  xabar ko'rish, broadcast, analytics, loglar, moderatsiya),
                  lekin OWNER-only amallarni BAJARA OLMAYDI.
* ``moderator`` — ko'rish + moderatsiya (ban/unban).  Sozlama, admin
                  boshqaruvi, broadcast YO'Q.
* ``viewer``    — FAQAT o'qish: analytics/statistika.  Hech qanday
                  destruktiv amal, sozlama, broadcast YO'Q.

MUHIM XAVFSIZLIK QOIDASI
------------------------
Ruxsat FAQAT serverda (``has_permission``) tekshiriladi.  Tugmani yashirish,
frontenddagi ``role``/``isAdmin`` maydonlari yoki qo'lda yasalgan callback
data HECH QACHON huquq bermaydi.

Noma'lum ruxsat nomi — ``False`` (fail-closed): imlo xatosi jimgina ruxsat
berib qo'ymasligi uchun.
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Rollar
# ---------------------------------------------------------------------------
ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MODERATOR = "moderator"
ROLE_VIEWER = "viewer"

#: Barcha haqiqiy rollar (quvvat bo'yicha o'sish tartibida).
VALID_ROLES: tuple[str, ...] = (ROLE_VIEWER, ROLE_MODERATOR, ROLE_ADMIN, ROLE_OWNER)

#: Kuch darajasi — "kim kimni boshqara oladi" qarorlari uchun.
ROLE_RANK: dict[str, int] = {
    ROLE_VIEWER: 10,
    ROLE_MODERATOR: 20,
    ROLE_ADMIN: 30,
    ROLE_OWNER: 40,
}

#: Runtime (panel orqali) berilishi MUMKIN bo'lgan rollar.
#: OWNER faqat env (ADMIN_ID / ADMIN_OWNER_IDS) orqali belgilanadi —
#: paneldan hech kim o'zini owner qilib ko'tara olmaydi.
MANAGED_ROLES: tuple[str, ...] = (ROLE_VIEWER, ROLE_MODERATOR, ROLE_ADMIN)

# ---------------------------------------------------------------------------
# Ruxsat nomlari (imlo xatolarini oldini olish uchun konstanta)
# ---------------------------------------------------------------------------
P_DASHBOARD_VIEW = "dashboard.view"
P_CONNECTIONS_VIEW = "connections.view"
P_CONNECTIONS_MANAGE = "connections.manage"
P_CONNECTIONS_DELETE = "connections.delete"
P_MESSAGES_VIEW = "messages.view"
P_LOGS_VIEW = "logs.view"
P_LOGS_CLEAR = "logs.clear"
P_SEARCH_USE = "search.use"
P_CACHE_VIEW = "cache.view"
P_CACHE_PURGE = "cache.purge"
P_CACHE_CLEAR = "cache.clear"
P_DATABASE_VIEW = "database.view"
P_HEALTH_VIEW = "health.view"
P_ANALYTICS_VIEW = "analytics.view"
P_ALERTS_VIEW = "alerts.view"
P_SETTINGS_VIEW = "settings.view"
P_SETTINGS_WRITE = "settings.write"
P_ADMINS_VIEW = "admins.view"
P_ADMINS_MANAGE = "admins.manage"
P_USERS_VIEW = "users.view"
P_USERS_MODERATE = "users.moderate"
P_USERS_MESSAGE = "users.message"
P_USERS_DELETE = "users.delete"
P_BROADCAST_VIEW = "broadcast.view"
P_BROADCAST_USE = "broadcast.use"

# ---------------------------------------------------------------------------
# RUXSAT MATRITSASI: ruxsat -> ruxsat berilgan rollar
# ---------------------------------------------------------------------------
PERMISSIONS: dict[str, tuple[str, ...]] = {
    # --- ko'rish (monitoring) -------------------------------------------
    P_DASHBOARD_VIEW: (ROLE_OWNER, ROLE_ADMIN, ROLE_MODERATOR),
    P_CONNECTIONS_VIEW: (ROLE_OWNER, ROLE_ADMIN, ROLE_MODERATOR),
    P_MESSAGES_VIEW: (ROLE_OWNER, ROLE_ADMIN, ROLE_MODERATOR),
    P_LOGS_VIEW: (ROLE_OWNER, ROLE_ADMIN),
    P_SEARCH_USE: (ROLE_OWNER, ROLE_ADMIN, ROLE_MODERATOR),
    P_CACHE_VIEW: (ROLE_OWNER, ROLE_ADMIN),
    P_DATABASE_VIEW: (ROLE_OWNER, ROLE_ADMIN),
    P_HEALTH_VIEW: (ROLE_OWNER, ROLE_ADMIN),
    P_ALERTS_VIEW: (ROLE_OWNER, ROLE_ADMIN),
    P_SETTINGS_VIEW: (ROLE_OWNER, ROLE_ADMIN),
    # --- analytics: barcha rollar (viewer ham FAQAT o'qish) -------------
    P_ANALYTICS_VIEW: (ROLE_OWNER, ROLE_ADMIN, ROLE_MODERATOR, ROLE_VIEWER),
    # --- ulanish operatsiyalari ----------------------------------------
    P_CONNECTIONS_MANAGE: (ROLE_OWNER, ROLE_ADMIN),
    P_CONNECTIONS_DELETE: (ROLE_OWNER,),
    # --- destruktiv tizim amallari (OWNER-only) ------------------------
    P_LOGS_CLEAR: (ROLE_OWNER,),
    P_CACHE_CLEAR: (ROLE_OWNER,),
    P_CACHE_PURGE: (ROLE_OWNER, ROLE_ADMIN),  # xavfsiz: faqat TTL bo'yicha
    P_SETTINGS_WRITE: (ROLE_OWNER,),
    # --- admin boshqaruvi (faqat OWNER) --------------------------------
    P_ADMINS_VIEW: (ROLE_OWNER,),
    P_ADMINS_MANAGE: (ROLE_OWNER,),
    # --- moderatsiya ---------------------------------------------------
    P_USERS_VIEW: (ROLE_OWNER, ROLE_ADMIN, ROLE_MODERATOR),
    P_USERS_MODERATE: (ROLE_OWNER, ROLE_ADMIN, ROLE_MODERATOR),
    # Userga paneldan bevosita xabar yuborish — faqat owner/admin.
    P_USERS_MESSAGE: (ROLE_OWNER, ROLE_ADMIN),
    # Userni operational bazadan o'chirish — faqat OWNER/ADMIN.
    P_USERS_DELETE: (ROLE_OWNER, ROLE_ADMIN),
    # --- reklama/broadcast ---------------------------------------------
    # VIEWER va MODERATOR broadcastga ruxsat OLMAYDI (talab).
    P_BROADCAST_VIEW: (ROLE_OWNER, ROLE_ADMIN),
    P_BROADCAST_USE: (ROLE_OWNER, ROLE_ADMIN),
}

ALL_PERMISSIONS: tuple[str, ...] = tuple(PERMISSIONS)


def normalize_role(role: Optional[str]) -> Optional[str]:
    """Rol nomini tekshiradi (noto'g'ri/bo'sh -> ``None``)."""
    if not role:
        return None
    value = str(role).strip().lower()
    return value if value in ROLE_RANK else None


def roles_for(permission: str) -> tuple[str, ...]:
    """Shu ruxsatni bergan rollar (noma'lum ruxsat -> bo'sh)."""
    return PERMISSIONS.get(permission, ())


def permissions_for(role: Optional[str]) -> frozenset[str]:
    """Rolga ruxsat berilgan BARCHA ruxsatlar (UI uchun ham)."""
    value = normalize_role(role)
    if value is None:
        return frozenset()
    return frozenset(p for p, roles in PERMISSIONS.items() if value in roles)


def role_has(role: Optional[str], permission: str) -> bool:
    """Rol shu ruxsatga egami?  (noma'lum ruxsat -> ``False``)"""
    value = normalize_role(role)
    if value is None:
        return False
    return value in roles_for(permission)


def has_permission(actor, permission: str) -> bool:
    """Markazlashtirilgan tekshiruv: ``has_permission(actor, permission)``.

    ``actor`` — rol nomi (``"owner"``) YOKI Telegram user id (``int``).
    User id berilganda rol ``app.services.admin_roles`` orqali (kechiktirilgan
    import bilan) ANIQLANADI — hech qachon chaqiruvchi bergan qiymatga
    ishonilmaydi.
    """
    if isinstance(actor, bool):  # bool - int ning ichki turi: xato
        return False
    if isinstance(actor, int):
        from app.services import admin_roles  # kechiktirilgan import (sikl yo'q)

        return role_has(admin_roles.role_of(actor), permission)
    return role_has(actor, permission)


def can_manage_roles(actor_role: Optional[str]) -> bool:
    """Rol boshqarishi mumkinmi (faqat OWNER)."""
    return normalize_role(actor_role) == ROLE_OWNER


def assignable_roles(actor_role: Optional[str]) -> tuple[str, ...]:
    """Actor bera oladigan rollar.

    * OWNER  -> viewer / moderator / admin (owner EMAS: u env orqali).
    * qolganlar -> bo'sh (hech narsa bera olmaydi).
    """
    return MANAGED_ROLES if can_manage_roles(actor_role) else ()


def can_act_on(actor_role: Optional[str], target_role: Optional[str]) -> bool:
    """Actor target ustidan amal bajara oladimi (QAT'IY kuch ustunligi)?

    OWNER hech qachon boshqa OWNER ustidan amal bajara olmaydi va
    hech kim (hatto admin) OWNERga tegmaydi — bu ``owner protection``.
    """
    actor = normalize_role(actor_role)
    target = normalize_role(target_role)
    if actor is None or target is None:
        return False
    if target == ROLE_OWNER:
        return False
    return ROLE_RANK[actor] > ROLE_RANK[target]


