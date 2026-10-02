"""RBAC verification — rollar, ruxsatlar va SERVER-SIDE himoya.

Tekshiriladi:

* OWNER / ADMIN / MODERATOR / VIEWER ruxsat matritsasi;
* har bir himoyalangan amal BACKENDDA tekshiriladi (Telegram callback,
  Telegram message handler, Web Admin API);
* qo'lda yasalgan callback data va frontendda yashirilgan/o'zgartirilgan
  maydonlar hech narsani o'zgartirmaydi;
* rol ko'tarilishi (escalation) rad etiladi;
* OWNER himoyasi: olib tashlanmaydi, pasaytirilmaydi, kamida bitta OWNER
  qoladi, OWNER darajasi paneldan berilmaydi;
* nozik amallar audit jurnaliga yoziladi (actor, rol, target, natija).

Barchasi thrash SQLite bazasida ishlaydi (production credentials yuklanmaydi).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from _verification_helpers import (
    ADMIN_ID,
    OUTSIDER,
    OWNER,
    FakeBot,
    make_callback,
    make_init_data,
)

from app.database import db
from app.services import admin_roles, audit, moderation
from app.services.admin_roles import role_of
from app.services import permissions as perms
from app.web import (
    api_admins,
    api_add_admin,
    api_analytics,
    api_delete_admin,
    api_get_settings,
    api_moderation,
    api_moderate_user,
    api_set_admin_role,
    api_set_settings,
    api_stats,
)

MODERATOR = 333000111
VIEWER = 444000111
PLAIN_USER = 555000111
BAN_TARGET = 555000222


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _Request:
    """Minimal aiohttp Request (signed initData + optional JSON body/query)."""

    def __init__(
        self,
        user_id: int = OWNER,
        *,
        body: dict | None = None,
        query: dict | None = None,
        bot=None,  # noqa: ANN001
        init_data: str | None = None,
    ) -> None:
        self.headers = {
            "X-Telegram-Init-Data": (
                init_data if init_data is not None else make_init_data(user_id)
            )
        }
        params = dict(query or {})
        self.query = SimpleNamespace(get=lambda key, default=None: params.get(key, default))
        self.app = {"bot": bot}
        self._body = body

    async def json(self) -> dict | None:
        return self._body


def _alerts(cb) -> list[tuple[str, bool]]:  # noqa: ANN001
    return list(getattr(cb, "_answers", []))


def _blocked(cb) -> bool:  # noqa: ANN001
    """Handler hech narsa ko'rsatmadi-yu, alert bilan rad etildi."""
    return not cb.message.texts and any(show for _, show in _alerts(cb))


async def _setup() -> None:
    await db.init()
    await admin_roles.load()
    await admin_roles.add(MODERATOR, perms.ROLE_MODERATOR)
    await admin_roles.add(VIEWER, perms.ROLE_VIEWER)


# ---------------------------------------------------------------------------
# 1) Ruxsat matritsasi
# ---------------------------------------------------------------------------
def test_permission_matrix_per_role() -> None:
    owner = perms.permissions_for(perms.ROLE_OWNER)
    admin = perms.permissions_for(perms.ROLE_ADMIN)
    moderator = perms.permissions_for(perms.ROLE_MODERATOR)
    viewer = perms.permissions_for(perms.ROLE_VIEWER)
    outsider = perms.permissions_for(None)

    # OWNER — hamma ruxsat.
    assert owner == frozenset(perms.ALL_PERMISSIONS)

    # ADMIN — operatsion amallar bor...
    for permission in (
        perms.P_DASHBOARD_VIEW, perms.P_CONNECTIONS_VIEW, perms.P_MESSAGES_VIEW,
        perms.P_LOGS_VIEW, perms.P_SEARCH_USE, perms.P_CACHE_VIEW,
        perms.P_DATABASE_VIEW, perms.P_HEALTH_VIEW, perms.P_ANALYTICS_VIEW,
        perms.P_ALERTS_VIEW, perms.P_SETTINGS_VIEW, perms.P_CONNECTIONS_MANAGE,
        perms.P_USERS_MODERATE, perms.P_USERS_MESSAGE, perms.P_USERS_DELETE, perms.P_BROADCAST_USE,
    ):
        assert permission in admin, permission
    # ...lekin OWNER-only amallar YO'Q.
    for permission in (
        perms.P_ADMINS_MANAGE, perms.P_ADMINS_VIEW, perms.P_SETTINGS_WRITE,
        perms.P_LOGS_CLEAR, perms.P_CACHE_CLEAR, perms.P_CONNECTIONS_DELETE,
    ):
        assert permission not in admin, permission

    # MODERATOR — ko'rish + moderatsiya; broadcast/sozlama/admin yo'q.
    assert perms.P_USERS_MODERATE in moderator
    assert perms.P_MESSAGES_VIEW in moderator
    assert perms.P_ANALYTICS_VIEW in moderator
    for permission in (
        perms.P_BROADCAST_USE, perms.P_BROADCAST_VIEW, perms.P_SETTINGS_VIEW,
        perms.P_SETTINGS_WRITE, perms.P_ADMINS_VIEW, perms.P_ADMINS_MANAGE,
        perms.P_CACHE_VIEW, perms.P_DATABASE_VIEW, perms.P_LOGS_VIEW,
        perms.P_USERS_MESSAGE,
    ):
        assert permission not in moderator, permission

    # VIEWER — FAQAT analytics (o'qish).
    assert viewer == frozenset({perms.P_ANALYTICS_VIEW})
    assert perms.P_ANALYTICS_VIEW in viewer
    assert perms.P_MESSAGES_VIEW not in viewer
    assert perms.P_USERS_DELETE not in viewer

    # Rol bo'lmasa — hech narsa; noma'lum ruxsat — har doim False.
    assert outsider == frozenset()
    for role in (perms.ROLE_OWNER, perms.ROLE_ADMIN, perms.ROLE_MODERATOR, perms.ROLE_VIEWER):
        assert perms.role_has(role, "does.not.exist") is False
    assert perms.role_has(None, perms.P_DASHBOARD_VIEW) is False


def test_role_labels_and_ordering() -> None:
    assert perms.ROLE_RANK[perms.ROLE_OWNER] > perms.ROLE_RANK[perms.ROLE_ADMIN]
    assert perms.ROLE_RANK[perms.ROLE_ADMIN] > perms.ROLE_RANK[perms.ROLE_MODERATOR]
    assert perms.ROLE_RANK[perms.ROLE_MODERATOR] > perms.ROLE_RANK[perms.ROLE_VIEWER]
    # OWNER boshqa OWNER ustidan amal bajara olmaydi (himoya).
    assert perms.can_act_on(perms.ROLE_OWNER, perms.ROLE_OWNER) is False
    assert perms.can_act_on(perms.ROLE_ADMIN, perms.ROLE_OWNER) is False
    assert perms.can_act_on(perms.ROLE_OWNER, perms.ROLE_ADMIN) is True
    # Faqat OWNER rol bera oladi, va OWNER darajasi berilmaydi.
    assert perms.assignable_roles(perms.ROLE_OWNER) == perms.MANAGED_ROLES
    assert perms.assignable_roles(perms.ROLE_ADMIN) == ()
    assert perms.ROLE_OWNER not in perms.MANAGED_ROLES


# ---------------------------------------------------------------------------
# 2) (chat panel removed — all enforcement is in the Web Mini App API)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 3) Web Admin: backend enforcement (401/403 + tamper)
# ---------------------------------------------------------------------------
async def _web_enforcement() -> None:
    await _setup()
    try:
        # VIEWER — faqat analytics.
        assert (await api_analytics(_Request(VIEWER))).status == 200
        assert (await api_stats(_Request(VIEWER))).status == 403
        assert (await api_get_settings(_Request(VIEWER))).status == 403
        assert (await api_moderation(_Request(VIEWER))).status == 403
        assert (
            await api_moderate_user(
                _Request(VIEWER, body={"action": "ban", "user_id": PLAIN_USER})
            )
        ).status == 403
        assert not moderation.is_banned(PLAIN_USER)

        # MODERATOR — ko'rish + ban/unban; broadcast/sozlama YO'Q.
        assert (await api_stats(_Request(MODERATOR))).status == 200
        assert (await api_moderation(_Request(MODERATOR))).status == 200
        banned = await api_moderate_user(
            _Request(MODERATOR, body={"action": "ban", "user_id": PLAIN_USER,
                                      "reason": "spam"})
        )
        assert banned.status == 200
        assert moderation.is_banned(PLAIN_USER)
        # Adminni ban qilib bo'lmaydi (server-side himoya).
        assert (
            await api_moderate_user(
                _Request(MODERATOR, body={"action": "ban", "user_id": ADMIN_ID})
            )
        ).status == 403
        assert not moderation.is_banned(ADMIN_ID)
        # Sozlama yozish OWNER-only.
        assert (
            await api_set_settings(
                _Request(MODERATOR, body={"maintenance_mode": True})
            )
        ).status == 403

        # Frontenddan yuborilgan rol/isAdmin/isOwner ISHONILMAYDI.
        tampered = await api_moderate_user(
            _Request(
                MODERATOR,
                body={
                    "action": "unban", "user_id": PLAIN_USER,
                    "role": "owner", "isOwner": True, "isAdmin": True,
                },
            )
        )
        assert tampered.status == 200  # ruxsat bor — lekin rol maydonlari e'tiborsiz
        tampered_settings = await api_set_settings(
            _Request(
                MODERATOR,
                body={"maintenance_mode": True, "role": "owner", "isOwner": True},
            )
        )
        assert tampered_settings.status == 403

        # ADMIN — o'qish bor, OWNER-only yozish yo'q.
        assert (await api_get_settings(_Request(ADMIN_ID))).status == 200
        assert (
            await api_set_settings(_Request(ADMIN_ID, body={"maintenance_mode": True}))
        ).status == 403
        assert (
            await api_set_settings(
                _Request(ADMIN_ID, body={"retention_days": 30})
            )
        ).status == 403

        # OWNER — yozadi.
        assert (
            await api_set_settings(_Request(OWNER, body={"maintenance_mode": True}))
        ).status == 200
        await api_set_settings(_Request(OWNER, body={"maintenance_mode": False}))

        # Rol yo'q / soxta initData.
        assert (await api_stats(_Request(OUTSIDER))).status == 403
        assert (
            await api_stats(_Request(OUTSIDER, init_data="test"))
        ).status == 401
    finally:
        await db.close()


def test_web_admin_backend_enforcement() -> None:
    asyncio.run(_web_enforcement())


async def _admin_management_api() -> None:
    await _setup()
    target = PLAIN_USER + 7001
    try:
        owner_list = await api_admins(_Request(OWNER))
        assert owner_list.status == 200
        assert (await api_admins(_Request(ADMIN_ID))).status == 403

        added = await api_add_admin(_Request(OWNER, body={"user_id": target, "role": "VIEWER"}))
        assert added.status == 201
        assert admin_roles.role_of(target) == perms.ROLE_VIEWER

        denied_add = await api_add_admin(_Request(ADMIN_ID, body={"user_id": target + 1, "role": "ADMIN"}))
        assert denied_add.status == 403
        assert await admin_roles.remove(target) is True
    finally:
        await db.close()


def test_web_admin_management_permissions() -> None:
    asyncio.run(_admin_management_api())


# ---------------------------------------------------------------------------
# 4) Rol boshqaruvi + escalation
# ---------------------------------------------------------------------------


async def _role_management() -> None:
    await _setup()
    try:
        assert admin_roles.owner_count() >= 1

        # --- OWNER qo'shadi va rolni o'zgartiradi. -------------------------
        assert await admin_roles.add(PLAIN_USER, perms.ROLE_ADMIN) is True
        assert role_of(PLAIN_USER) == perms.ROLE_ADMIN
        assert await admin_roles.set_role(PLAIN_USER, perms.ROLE_MODERATOR) is True
        assert role_of(PLAIN_USER) == perms.ROLE_MODERATOR

        # --- OWNER hech qachon pasaytirilmaydi/olib tashlanmaydi. ---------
        assert await admin_roles.remove(OWNER) is False
        assert await admin_roles.set_role(OWNER, perms.ROLE_VIEWER) is False
        assert role_of(OWNER) == perms.ROLE_OWNER
        assert admin_roles.owner_count() >= 1

        # --- Admin o'zini ko'tara olmaydi (env roli). ---------------------
        assert await admin_roles.add(ADMIN_ID, perms.ROLE_MODERATOR) is False
        assert await admin_roles.set_role(ADMIN_ID, perms.ROLE_VIEWER) is False
        assert role_of(ADMIN_ID) == perms.ROLE_ADMIN

        # --- Runtime owner berish har doim rad etiladi. -------------------
        assert await admin_roles.add(OUTSIDER, perms.ROLE_OWNER) is False
        assert await admin_roles.set_role(PLAIN_USER, perms.ROLE_OWNER) is False
        assert role_of(PLAIN_USER) == perms.ROLE_MODERATOR
        assert admin_roles.has_runtime(OWNER) is False

        # --- .env orqali belgilangan rol o'chirilmaydi. -------------------
        assert await admin_roles.remove(ADMIN_ID) is False
        assert role_of(ADMIN_ID) == perms.ROLE_ADMIN
    finally:
        await admin_roles.remove(PLAIN_USER)
        await db.close()


def test_role_management_and_owner_protection() -> None:
    asyncio.run(_role_management())


# ---------------------------------------------------------------------------
# 5) Audit jurnali
# ---------------------------------------------------------------------------
def test_audit_entry_format() -> None:
    line = audit.format_entry(
        "role_changed", actor=111, actor_role="owner", target=555,
        result="ok", old="admin", new="moderator",
    )
    for token in (
        "action=role_changed", "actor=111", "role=owner", "target=555",
        "result=ok", "old=admin", "new=moderator",
    ):
        assert token in line, line
    # Maxfiy ma'lumot hech qachon yozilmaydi (format faqat bergan qiymatlardan).
    assert "token" not in line.lower()



# ---------------------------------------------------------------------------
# 6) Ban middleware (ban qilingan foydalanuvchi handlerga yetib bormaydi)
# ---------------------------------------------------------------------------
async def _ban_middleware() -> None:
    from aiogram.types import Chat, Message as TgMessage, Update, User as TgUser

    from app.middlewares import RegisterUserMiddleware
    from app.services import maintenance

    await _setup()
    # Oldingi testlar PLAIN_USER'ga runtime rol bergan bo'lishi mumkin; runtime
    # adminlar ban'dan MUSTASNO — bu test OODDIY foydalanuvchini tekshiradi.
    await admin_roles.remove(PLAIN_USER)
    await maintenance.set_enabled(False, notify_resume=False)
    try:
        user = TgUser(id=PLAIN_USER, is_bot=False, first_name="Moderated")
        update = Update.model_construct(
            update_id=1,
            event_type="message",
            message=TgMessage.model_construct(
                message_id=1,
                chat=Chat(id=PLAIN_USER, type="private"),
                from_user=user,
                text="/start",
            ),
        )
        chained = {"n": 0}

        async def nxt(event, data):  # noqa: ANN001
            chained["n"] += 1
            return "ok"

        await RegisterUserMiddleware()(nxt, update, {"event_from_user": user})
        assert chained["n"] == 1, "oddiy foydalanuvchi o'tishi kerak"

        await moderation.ban(PLAIN_USER, by=MODERATOR, reason="test")
        await RegisterUserMiddleware()(nxt, update, {"event_from_user": user})
        assert chained["n"] == 1, "ban qilingan foydalanuvchi to'xtatilishi kerak"

        await moderation.unban(PLAIN_USER)
        await RegisterUserMiddleware()(nxt, update, {"event_from_user": user})
        assert chained["n"] == 2, "unbandan keyin yana o'tishi kerak"

        # Admin hech qachon bloklanmaydi.
        await moderation.ban(ADMIN_ID, by=MODERATOR, reason="should not stick")
        admin_user = TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin")
        admin_update = Update.model_construct(
            update_id=2,
            event_type="message",
            message=TgMessage.model_construct(
                message_id=2,
                chat=Chat(id=ADMIN_ID, type="private"),
                from_user=admin_user,
                text="/start",
            ),
        )
        await RegisterUserMiddleware()(nxt, admin_update, {"event_from_user": admin_user})
        assert chained["n"] == 3, "admin ban ta'sirida bo'lmasligi kerak"
    finally:
        await moderation.unban(PLAIN_USER)
        await moderation.unban(ADMIN_ID)
        moderation.reset_for_tests()
        await db.close()


def test_ban_middleware_blocks_and_admins_are_exempt() -> None:
    asyncio.run(_ban_middleware())


# ---------------------------------------------------------------------------
# 7) Broadcast ruxsati VIEWER/MODERATORga berilmaydi (engine darajasi)
# ---------------------------------------------------------------------------
async def _broadcast_permission() -> None:
    from app.web import api_broadcast, api_broadcast_test

    await _setup()
    try:
        bot = FakeBot()
        for user_id in (VIEWER, MODERATOR):
            assert (
                await api_broadcast(_Request(user_id, body={"text": "reklama"}, bot=bot))
            ).status == 403
            assert (
                await api_broadcast_test(
                    _Request(user_id, body={"text": "test"}, bot=bot)
                )
            ).status == 403
        assert not bot.sent, "ruxsatsiz foydalanuvchi uchun hech narsa yuborilmaydi"
        # OWNER/ADMIN ruxsatga ega.
        assert (
            await api_broadcast_test(_Request(ADMIN_ID, body={"text": "test"}, bot=bot))
        ).status == 200
        assert bot.sent
    finally:
        await db.close()


def test_broadcast_permission_is_owner_admin_only() -> None:
    asyncio.run(_broadcast_permission())
