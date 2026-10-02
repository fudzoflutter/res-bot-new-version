from __future__ import annotations

import asyncio
from types import SimpleNamespace

from _verification_helpers import ADMIN_ID, OWNER, JsonRequest

from app.database import db
from app.services import admin_roles, connection_verify, moderation
from app.services import permissions as perms
from app.web import api_delete_user, api_users, api_verify_connections


def _request(user_id=OWNER, *, query=None, bot=None):
    return JsonRequest(user_id=user_id, query=query or {}, bot=bot)


class VerifyBot:
    def __init__(self, states):
        self.states = states
        self.sent = []

    async def get_business_connection(self, *, business_connection_id):
        value = self.states[business_connection_id]
        if isinstance(value, Exception):
            raise value
        return value

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


async def _run():
    await db.init()
    await admin_roles.load()
    try:
        uid = 9911001
        await db.upsert_user(uid, "test_user", "Test", None)
        await db.upsert_connection("bc_test_1", uid, True, uid)
        await db.add_event(uid, "text", "payload", uid, "chat", 1, uid + 100)
        await db.broadcast_seed("b_test", [uid])

        # Users endpoint is DB-backed.
        res = await api_users(_request(ADMIN_ID))
        assert res.status == 200

        # Direct facade delete is the real data-path assertion.
        assert await db.delete_user(uid) is True
        assert await db.get_user(uid) is None
        assert await db.connections_for_user(uid) == []

        # Permission: viewer cannot delete.
        assert not perms.role_has(perms.ROLE_VIEWER, perms.P_USERS_DELETE)

        # Connection verify: remote disabled state must reconcile DB + notify.
        uid2 = 9911002
        await db.upsert_user(uid2, "conn_user", "Conn", None)
        await db.upsert_connection("bc_test_2", uid2, True, uid2)
        remote = SimpleNamespace(is_enabled=False, user_chat_id=uid2, user=SimpleNamespace(id=uid2, username="conn_user", first_name="Conn", last_name=None))
        bot = VerifyBot({"bc_test_2": remote})
        connection_verify.configure_bot(bot)
        result = await connection_verify.verify_connection("bc_test_2", notify=True)
        assert result["changed"] is True
        row = await db.get_connection("bc_test_2")
        assert row["is_enabled"] is False
        assert bot.sent and bot.sent[-1][0] == uid2
    finally:
        connection_verify.reset_for_tests()
        moderation.reset_for_tests()
        admin_roles.reset_for_tests()
        await db.close()


def test_user_management_and_connection_reconciliation():
    asyncio.run(_run())
