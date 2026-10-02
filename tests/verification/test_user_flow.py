"""User-flow verification: /start, new user, business connections, messages,
media, reconnect, restart/recovery, duplicate updates and error resilience.

Runs on a throwaway SQLite database; Telegram is faked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from _verification_helpers import (
    BusinessConn,
    Deleted,
    FakeBot,
    IncomingMessage,
    file_ref,
)

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.database import db
from app.handlers import business as bh
from app.handlers import user as uh
from app.middlewares import RegisterUserMiddleware
from app.services import reporter as rep

CONN = "bc_flow_0001"
_STATE_STORAGE = MemoryStorage()


def _state(user_id: int) -> FSMContext:
    return FSMContext(
        storage=_STATE_STORAGE,
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


def _clear_caches() -> None:
    rep.clear_instant_cache()
    rep.clear_seen_events()
    rep.invalidate_connection(None)


# ---------------------------------------------------------------------------
# /start + new-user registration
# ---------------------------------------------------------------------------
async def _start_flow() -> None:
    new_user = 940001
    await db.init()
    # Hold mode holati BAZADA saqlanadi, shuning uchun modullar tartibiga
    # bog'liq bo'lmaslik uchun bu yerda aniq OFF qilinadi.
    from app.services import maintenance

    await maintenance.set_enabled(False, notify_resume=False)
    try:
        msg = IncomingMessage(1, new_user, text="/start")
        await uh.cmd_start(msg, _state(new_user))
        assert msg.answers, "/start rendered nothing"
        joined = " ".join(msg.answers).lower()
        assert "tasdiq" not in joined and "pending" not in joined, (
            "access-approval gate must not exist"
        )

        # A brand-new user is registered by the outer middleware (prod path).
        from aiogram.types import Chat, Message as TgMessage, Update, User as TgUser

        newcomer = 940011
        chained = {"n": 0}

        async def nxt(event, data):  # noqa: ANN001
            chained["n"] += 1
            return "ok"

        user = TgUser(id=newcomer, is_bot=False, first_name="New", username="newbie")
        update = Update.model_construct(
            update_id=1,
            event_type="message",
            message=TgMessage.model_construct(
                message_id=5, chat=Chat(id=newcomer, type="private"),
                from_user=user, text="/start",
            ),
        )
        await RegisterUserMiddleware()(nxt, update, {"event_from_user": user})
        for _ in range(100):
            if await db.get_user(newcomer):
                break
            await asyncio.sleep(0.02)
        assert await db.get_user(newcomer) is not None
        assert chained["n"] == 1
    finally:
        await db.close()


def test_start_and_new_user_registration() -> None:
    asyncio.run(_start_flow())


# ---------------------------------------------------------------------------
# connection + incoming/edited/deleted + media
# ---------------------------------------------------------------------------
async def _message_flow() -> None:
    owner, partner = 940101, 940099
    await db.init()
    _clear_caches()
    try:
        bot = FakeBot()
        await bh.on_connection(BusinessConn(CONN, owner), bot)
        row = await db.get_connection(CONN)
        assert row is not None and row.get("is_enabled")
        assert any(chat == owner for _, chat in bot.sent), "owner not notified"
        logs = await db.recent_activity_logs(limit=20)
        assert any(l["event_type"] == "connection_created" for l in logs)

        # duplicate connection update is deduped
        before = await db.count_activity_logs()
        await bh.on_connection(BusinessConn(CONN, owner), bot)
        assert await db.count_activity_logs() == before

        # incoming: cached silently, never forwarded
        _clear_caches()
        bot = FakeBot()
        incoming = IncomingMessage(101, partner, text="salom dunyo", connection_id=CONN)
        await bh.on_business_message(incoming, bot)
        entry = rep.recall(555001, 101)
        assert entry is not None and entry["details"] == "salom dunyo"
        assert not any(chat == owner for _, chat in bot.sent)

        # edited: reported to the owner
        bot = FakeBot()
        await bh.on_edited(
            IncomingMessage(101, partner, text="salom dunyo v2", connection_id=CONN), bot
        )
        assert any(chat == owner for _, chat in bot.sent)

        # deleted: original text reported
        bot = FakeBot()
        await bh.on_deleted(Deleted([101], connection_id=CONN), bot)
        assert any(chat == owner for _, chat in bot.sent)

        # media: cached, re-sent on delete
        _clear_caches()
        bot = FakeBot()
        await bh.on_business_message(
            IncomingMessage(202, partner, photo=[file_ref("p1")], connection_id=CONN), bot
        )
        assert rep.recall(555001, 202) is not None
        bot = FakeBot()
        await bh.on_deleted(Deleted([202], connection_id=CONN), bot)
        assert any(kind in ("photo", "video", "document") for kind, _ in bot.sent)
    finally:
        await db.close()


def test_business_message_media_flow() -> None:
    asyncio.run(_message_flow())


# ---------------------------------------------------------------------------
# reconnect + restart/recovery
# ---------------------------------------------------------------------------
async def _reconnect_and_recovery() -> None:
    owner, partner, conn = 940201, 940299, "bc_flow_0002"
    await db.init()
    _clear_caches()
    try:
        bot = FakeBot()
        # REGRESSION (reconnect dedupe bug): enabled -> disabled -> enabled
        # ketma-ketligi DEDUPE_TTL_SECONDS (600 s) ICHIDA bo'lsa ham HAR BIR
        # holat o'zgarishi ishlanadi.  Ilgari oxirgi ``enabled`` "bir xil
        # holat qayta keldi" deb tashlab yuborilardi.
        await bh.on_connection(BusinessConn(conn, owner), bot)
        assert (await db.get_connection(conn)).get("is_enabled"), "first enable lost"
        await bh.on_connection(BusinessConn(conn, owner, enabled=False), bot)
        row = await db.get_connection(conn)
        assert row is not None and not row.get("is_enabled"), "disable dropped"
        await bh.on_connection(BusinessConn(conn, owner, enabled=True), bot)
        row = await db.get_connection(conn)
        assert row is not None and row.get("is_enabled"), (
            "reconnect within the 600s dedupe window was dropped"
        )

        # Takroriy update (AYNAN bir xil holat qayta keldi) baribir YUTILADI.
        before = await db.count_activity_logs()
        await bh.on_connection(BusinessConn(conn, owner, enabled=True), bot)
        assert await db.count_activity_logs() == before, "duplicate not suppressed"

        # restart: in-memory cache gone, DB copy must still drive the report
        _clear_caches()
        bot = FakeBot()
        await bh.on_business_message(
            IncomingMessage(303, partner, text="recover me", connection_id=conn), bot
        )
        rep.clear_instant_cache()  # simulate process restart (cache/TTL loss)
        bot = FakeBot()
        await bh.on_deleted(Deleted([303], connection_id=conn), bot)
        assert any(chat == owner for _, chat in bot.sent), "DB fallback failed"
    finally:
        await db.close()


def test_reconnect_and_restart_recovery() -> None:
    asyncio.run(_reconnect_and_recovery())


# ---------------------------------------------------------------------------
# state-aware connection dedupe (regression, unit level)
# ---------------------------------------------------------------------------
def test_connection_state_dedupe_semantics() -> None:
    """Holat o'zgarishi yutilmaydi; AYNAN bir xil holat yutiladi."""
    rep.clear_connection_states()
    cid = "bc_dedupe_unit"
    assert rep.mark_connection_state(cid, True) is False   # birinchi enable
    assert rep.mark_connection_state(cid, True) is True    # aynan takroriy
    assert rep.mark_connection_state(cid, False) is False  # haqiqiy o'zgarish
    assert rep.mark_connection_state(cid, False) is True   # aynan takroriy
    assert rep.mark_connection_state(cid, True) is False   # reconnect (TTL ichida)
    assert rep.mark_connection_state(cid, True) is True    # aynan takroriy
    # Empty connection id is never treated as a duplicate.
    assert rep.mark_connection_state("", True) is False
    assert rep.mark_connection_state("", True) is False
    rep.clear_connection_states()


# ---------------------------------------------------------------------------
# error resilience
# ---------------------------------------------------------------------------
async def _error_resilience() -> None:
    await db.init()
    try:
        # Telegram API failure while notifying must not break the flow
        bot = FakeBot()
        bot.forbidden.add(940002)
        await bh.on_connection(BusinessConn("bc_flow_err_0009", 940002), bot)
        assert await db.get_connection("bc_flow_err_0009") is not None

        # DB error while reading settings -> maintenance check degrades to False
        async def boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("db down")

        with patch.object(db, "get_setting", boom):
            assert await bh._maintenance_on() is False

        # reporter failure is swallowed by _safe_report
        class BoomReporter:
            def __init__(self, bot):  # noqa: ANN001
                pass

            async def report_incoming(self, message):  # noqa: ANN001
                raise RuntimeError("db down")

        with patch.object(bh, "Reporter", BoomReporter):
            await bh.on_business_message(
                IncomingMessage(404, 940099, text="x", connection_id=CONN), FakeBot()
            )
    finally:
        await db.close()


def test_error_resilience() -> None:
    asyncio.run(_error_resilience())
