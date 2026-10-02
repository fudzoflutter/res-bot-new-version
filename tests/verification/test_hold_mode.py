"""Hold (maintenance) mode verification.

Covers the deployment-safety requirements:

* persistent state (survives process restart — it lives in the database)
* toggling is effective immediately (no stale cache window)
* normal users are blocked and told why; admin/owner stay fully usable
* business-connection and broadcast gating
* Telegram admin panel and Web Admin share ONE state
* optional resume notification (connected users only, never a mass broadcast)

Runs on a throwaway SQLite database; Telegram is faked, nothing goes to the
network, and the session never loads production credentials (tests/conftest.py).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from _verification_helpers import (
    ADMIN_ID,
    BusinessConn,
    FakeBot,
    JsonRequest,
    OUTSIDER,
    OWNER,
    make_callback,
)

from aiogram.types import Chat, Message as TgMessage, Update, User as TgUser

from app import web as web_mod
from app.database import db
from app.handlers import business as bh
from app.middlewares import RegisterUserMiddleware
from app.services import maintenance, reporter as rep
from app.utils.tasks import drain

_UNIQUE = 950000  # keep this module's ids away from the other suites


def _message_update(user_id: int, *, text: str = "/start") -> tuple[Update, TgUser]:
    user = TgUser(id=user_id, is_bot=False, first_name="U", username=f"u{user_id}")
    update = Update.model_construct(
        update_id=user_id,
        event_type="message",
        message=TgMessage.model_construct(
            message_id=1,
            chat=Chat(id=user_id, type="private"),
            from_user=user,
            text=text,
        ),
    )
    return update, user


# ---------------------------------------------------------------------------
# 1. Persistent state + immediate effectiveness
# ---------------------------------------------------------------------------
async def _persistence() -> None:
    await db.init()
    try:
        await maintenance.set_enabled(False, notify_resume=False)
        assert await maintenance.is_enabled(force=True) is False

        # Warm the cache, then enable: the change must be visible AT ONCE
        # (a stale cache would let a just-banned user keep using the bot).
        assert await maintenance.is_enabled() is False
        await maintenance.set_enabled(True, notify_resume=False)
        assert await maintenance.is_enabled() is True, "cache not invalidated on write"
        assert await db.get_setting(maintenance.SETTING_KEY, "0") == "1"

        # RESTART SIMULATION: RAM cache and bot handle are gone; the database
        # copy must still say "hold mode ON".
        maintenance.reset_for_tests()
        assert await maintenance.is_enabled() is True, (
            "hold mode must survive process/container restart"
        )

        await maintenance.set_enabled(False, notify_resume=False)
        assert await maintenance.is_enabled(force=True) is False
        assert await db.get_setting(maintenance.SETTING_KEY, "0") == "0"
    finally:
        maintenance.reset_for_tests()
        await db.close()


def test_hold_mode_is_persistent_and_immediate() -> None:
    asyncio.run(_persistence())


# ---------------------------------------------------------------------------
# 2. Middleware gate: users blocked, admins/owner untouched
# ---------------------------------------------------------------------------
async def _middleware_gate() -> None:
    await db.init()
    try:
        await maintenance.set_enabled(True, notify_resume=False)
        mw = RegisterUserMiddleware()
        chained = {"n": 0}

        async def nxt(event, data):  # noqa: ANN001
            chained["n"] += 1
            return "ok"

        async def drive(uid: int) -> int:
            update, user = _message_update(uid)
            chained["n"] = 0
            await mw(nxt, update, {"event_from_user": user})
            return chained["n"]

        answers: list[str] = []

        async def fake_answer(self, text=None, **kwargs):  # noqa: ANN001
            answers.append(text or "")

        # --- normal user: NOT processed, told about the maintenance ---------
        with patch.object(TgMessage, "answer", fake_answer):
            assert await drive(OUTSIDER) == 0, "normal user must be paused"
        assert any("texnik xizmat" in a.lower() for a in answers), answers

        # --- admin + owner: still fully usable -------------------------------
        assert await drive(ADMIN_ID) == 1, "admin must stay usable during hold mode"
        assert await drive(OWNER) == 1, "owner must stay usable during hold mode"

        # --- callbacks are blocked too (with an alert) ----------------------
        cb = make_callback(OUTSIDER, "some:callback")
        chained["n"] = 0
        await mw(nxt, cb, {"event_from_user": cb.from_user})
        assert chained["n"] == 0
        assert cb._answers and cb._answers[-1][1] is True, cb._answers

        # --- business messages are paused by the maintenance handler itself.
        # Middleware may pass the tracking update through, but the business
        # handler must not process/report it while hold mode is active.
    finally:
        await drain()
        maintenance.reset_for_tests()
        await db.close()


def test_middleware_gate_blocks_users_not_admins() -> None:
    asyncio.run(_middleware_gate())


# ---------------------------------------------------------------------------
# 3. Business connections: new ones refused, existing ones untouched
# ---------------------------------------------------------------------------
async def _business_connection_gate() -> None:
    await db.init()
    try:
        bot = FakeBot()
        new_conn = f"bc_hold_new_{_UNIQUE}"
        known_conn = f"bc_hold_known_{_UNIQUE}"
        new_owner = _UNIQUE + 1
        known_owner = _UNIQUE + 2

        await maintenance.set_enabled(True, notify_resume=False)
        await bh.on_connection(BusinessConn(new_conn, new_owner), bot)

        row = await db.get_connection(new_conn)
        assert row is not None and not row.get("is_enabled"), (
            "a NEW connection must be stored disabled during hold mode"
        )
        assert any(chat == new_owner for _, chat in bot.sent), "owner not notified"

        logs = await db.recent_activity_logs(50)
        assert any(r.get("event_type") == "connection_rejected" for r in logs), (
            "rejection must be recorded in the activity log"
        )

        # An ALREADY-KNOWN connection keeps working during hold mode.
        await maintenance.set_enabled(False, notify_resume=False)
        rep.clear_seen_events()
        await bh.on_connection(BusinessConn(known_conn, known_owner), bot)
        rep.clear_seen_events()
        await maintenance.set_enabled(True, notify_resume=False)
        await bh.on_connection(BusinessConn(known_conn, known_owner, enabled=False), bot)
        rep.clear_seen_events()
        await bh.on_connection(BusinessConn(known_conn, known_owner, enabled=True), bot)

        row = await db.get_connection(known_conn)
        assert row is not None and row.get("is_enabled"), (
            "existing connections must not be broken by hold mode"
        )
    finally:
        rep.clear_seen_events()
        maintenance.reset_for_tests()
        await db.close()


def test_business_connection_gate() -> None:
    asyncio.run(_business_connection_gate())


# ---------------------------------------------------------------------------
# 4. One state shared by the Telegram panel and the Web Admin
# ---------------------------------------------------------------------------


async def _shared_state() -> None:
    await db.init()
    try:
        await maintenance.set_enabled(False, notify_resume=False)

        # Web Admin (owner) turns hold mode ON and reads it back
        response = await web_mod.api_set_settings(
            JsonRequest({"maintenance_mode": True}, user_id=OWNER)
        )
        assert response.status == 200
        assert await db.get_setting(maintenance.SETTING_KEY, "0") == "1"
        assert await maintenance.is_enabled() is True
        response = await web_mod.api_get_settings(JsonRequest(user_id=OWNER))
        assert json.loads(response.body)["maintenance_mode"] is True

        # A non-owner ADMIN may not change it (server-side check)
        assert (
            await web_mod.api_set_settings(
                JsonRequest({"maintenance_mode": False}, user_id=ADMIN_ID)
            )
        ).status == 403
        assert await db.get_setting(maintenance.SETTING_KEY, "0") == "1"

        # Owner turns it off again
        response = await web_mod.api_set_settings(
            JsonRequest({"maintenance_mode": False}, user_id=OWNER)
        )
        assert response.status == 200
        assert await maintenance.is_enabled() is False
        assert await db.get_setting(maintenance.SETTING_KEY, "0") == "0"
    finally:
        maintenance.reset_for_tests()
        await db.close()


def test_single_persistent_state_via_web_admin() -> None:
    asyncio.run(_shared_state())


# ---------------------------------------------------------------------------
# 5. Broadcast protection + resume notification
# ---------------------------------------------------------------------------
async def _broadcast_and_resume() -> None:
    await db.init()
    try:
        connected = _UNIQUE + 11     # has an ENABLED business connection
        not_connected = _UNIQUE + 12  # registered only
        await db.upsert_user(connected, f"u{connected}", "Connected", None)
        await db.upsert_user(not_connected, f"u{not_connected}", "Plain", None)
        await db.upsert_connection(
            f"bc_hold_notify_{_UNIQUE}", connected, True, connected
        )

        web_mod._broadcast_state.update({"running": False, "sent": 0, "failed": 0})
        bot = FakeBot()

        # Hold mode ON -> a new broadcast cannot start (503, not accepted)
        await maintenance.set_enabled(True, notify_resume=False)
        response = await web_mod.api_broadcast(
            JsonRequest({"text": "salom"}, bot=bot)
        )
        assert response.status == 503, response.status
        assert web_mod.broadcast_snapshot()["running"] is False
        assert not bot.sent, "no message may go out while hold mode is on"

        # A broadcast already running is NOT killed by hold mode (no silent
        # termination and no lost progress) — verified separately in
        # test_broadcast.py; here we only assert the gate blocks NEW starts.

        # Maintenance notifications are now mandatory and go to ALL registered users.
        maintenance.configure_notifier(bot)
        assert await maintenance.resume_notification_enabled() is True
        bot.sent.clear()
        await maintenance.set_enabled(True, notify_resume=False)
        await maintenance.set_enabled(False, await_notifications=True)
        recipients = {chat for _, chat in bot.sent}
        assert connected in recipients, recipients
        assert not_connected in recipients, "registered users must be notified even without a business connection"
        assert all(kind == "message" for kind, _ in bot.sent)

        # Turning OFF when already OFF sends nothing.
        bot.sent.clear()
        await maintenance.set_enabled(False)
        assert not bot.sent
    finally:
        await db.set_setting(maintenance.RESUME_NOTIFY_KEY, "0")
        maintenance.reset_for_tests()
        await db.close()


def test_broadcast_blocked_and_all_user_maintenance_notifications() -> None:
    asyncio.run(_broadcast_and_resume())


# ---------------------------------------------------------------------------
# 6. Hold mode must never silently terminate a RUNNING broadcast
# ---------------------------------------------------------------------------
async def _hold_does_not_kill_running_broadcast() -> None:
    await db.init()
    try:
        recipients = [_UNIQUE + 21, _UNIQUE + 22, _UNIQUE + 23]
        for uid in recipients:
            await db.upsert_user(uid, f"u{uid}", "User", None)

        await maintenance.set_enabled(False, notify_resume=False)
        web_mod._broadcast_state.update({"running": False, "sent": 0, "failed": 0})
        bot = FakeBot()

        assert (
            await web_mod.api_broadcast(JsonRequest({"text": "salom"}, bot=bot))
        ).status == 202

        # Hold mode goes ON while the broadcast is still running.
        await maintenance.set_enabled(True, notify_resume=False)
        assert (
            await web_mod.api_broadcast(JsonRequest({"text": "yana"}, bot=bot))
        ).status == 503, "a new broadcast must not start during hold mode"

        # The already-running one keeps its progress and finishes accurately.
        for _ in range(500):
            if not web_mod.broadcast_snapshot()["running"]:
                break
            await asyncio.sleep(0.02)
        final = web_mod.broadcast_snapshot()
        assert final["running"] is False, "running broadcast must not be stranded"
        assert final["total"] >= len(recipients), final
        assert final["sent"] + final["failed"] == final["total"]
        assert final["sent"] >= 1
        assert any(chat in recipients for _, chat in bot.sent)
    finally:
        await maintenance.set_enabled(False, notify_resume=False)
        maintenance.reset_for_tests()
        await db.close()


def test_hold_mode_never_kills_a_running_broadcast() -> None:
    asyncio.run(_hold_does_not_kill_running_broadcast())
