"""Broadcast verification: retry semantics, 202/409 lifecycle, real progress
polling and accurate final counts.

Uses the real ``api_broadcast`` handler with a network-free bot.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from _verification_helpers import FakeBot, OWNER, make_init_data

from app import web as web_mod
from app.config import settings
from app.database import db
from app.utils import telegram_api

# Unique recipient ids (other verification modules may add their own users).
_RETRY_429 = 7101
_NETWORK_FAIL = 7102
_PERMANENT_FAIL = 7103
_OTHER_RECIPIENTS = (7104, 7105, 7106)


class _Request:
    """Minimal aiohttp Request for calling the handler directly."""

    def __init__(self, body: dict, bot) -> None:  # noqa: ANN001
        self.headers = {"X-Telegram-Init-Data": make_init_data(OWNER)}
        self.query = SimpleNamespace(get=lambda *a, **k: None)
        self.app = {"bot": bot}
        self._body = body

    async def json(self) -> dict:
        return self._body


# ---------------------------------------------------------------------------
# telegram_api.call retry semantics
# ---------------------------------------------------------------------------
async def _retry_semantics() -> None:
    from aiogram.exceptions import (
        TelegramBadRequest,
        TelegramConflictError,
        TelegramNetworkError,
        TelegramRetryAfter,
    )

    calls = {"n": 0}

    async def flood():
        calls["n"] += 1
        if calls["n"] < 2:
            raise TelegramRetryAfter(method="m", message="429", retry_after=0)
        return "ok"

    assert await telegram_api.call("t429", flood, attempts=3, base_delay=0.01) == "ok"
    assert telegram_api.stats.retry_after_count >= 1

    net = {"n": 0}

    async def flaky_network():
        net["n"] += 1
        if net["n"] < 2:
            raise TelegramNetworkError(method="m", message="net down")
        return "ok"

    assert await telegram_api.call("tnet", flaky_network, attempts=3, base_delay=0.01) == "ok"
    assert telegram_api.stats.network_errors >= 1

    async def conflict():
        raise TelegramConflictError(method="m", message="409")

    try:
        await telegram_api.call("t409", conflict, attempts=3, base_delay=0.01)
    except TelegramConflictError:
        pass
    else:  # pragma: no cover
        raise AssertionError("409 conflict must NOT be retried")

    async def permanent():
        raise TelegramBadRequest(method="m", message="bad")

    try:
        await telegram_api.call("tbad", permanent, attempts=3, base_delay=0.01)
    except TelegramBadRequest:
        pass
    else:  # pragma: no cover
        raise AssertionError("BadRequest must NOT be retried")


def test_telegram_api_retry_semantics() -> None:
    asyncio.run(_retry_semantics())


# ---------------------------------------------------------------------------
# broadcast lifecycle
# ---------------------------------------------------------------------------
async def _lifecycle() -> None:
    await db.init()
    try:
        for uid in (_RETRY_429, _NETWORK_FAIL, _PERMANENT_FAIL, *_OTHER_RECIPIENTS):
            await db.upsert_user(uid, f"u{uid}", "User", None)

        bot = FakeBot()
        bot.retry429[_RETRY_429] = 1  # 429 once, then succeeds
        bot.netfail[_NETWORK_FAIL] = 1  # network error once, then succeeds
        bot.forbidden.add(_PERMANENT_FAIL)  # always fails -> must be isolated

        web_mod._broadcast_state.update({"running": False, "sent": 0, "failed": 0})

        # first request -> 202 (accepted, not "delivered")
        response = await web_mod.api_broadcast(_Request({"text": "salom"}, bot))
        assert response.status == 202

        recipients = [
            int(u["user_id"])
            for u in await db.all_users()
            if int(u["user_id"]) != settings.admin_id  # owner is never a recipient
        ]
        assert json.loads(response.body)["total"] == len(recipients)

        # duplicate while running -> 409
        assert (await web_mod.api_broadcast(_Request({"text": "again"}, bot))).status == 409

        # progress polling: observe running=True, then finished
        saw_running = False
        for _ in range(500):
            snapshot = web_mod.broadcast_snapshot()
            if snapshot["running"]:
                saw_running = True
            else:
                break
            await asyncio.sleep(0.02)
        final = web_mod.broadcast_snapshot()

        assert saw_running, "never observed running=True (no progress state)"
        assert final["running"] is False
        assert final["total"] == len(recipients)
        assert final["failed"] == 1, final  # only the permanently forbidden one
        assert final["sent"] == len(recipients) - 1
        assert final["sent"] + final["failed"] == final["total"]
        assert final["last_error"]
        assert _NETWORK_FAIL in {chat for _, chat in bot.sent}

        # Final progress is PERSISTED (restart/deploy survives).
        persisted = json.loads(
            await db.get_setting(web_mod._BROADCAST_STATE_KEY, "{}")
        )
        assert persisted["running"] is False
        assert persisted["total"] == final["total"]
        assert persisted["sent"] == final["sent"]
        assert persisted["failed"] == final["failed"]
    finally:
        await db.close()


def test_broadcast_lifecycle_and_counts() -> None:
    asyncio.run(_lifecycle())


# ---------------------------------------------------------------------------
# restart / deploy recovery
# ---------------------------------------------------------------------------
async def _recovery() -> None:
    await db.init()
    try:
        # 1) A broadcast that was RUNNING when the process died.
        stored = {
            "running": True,
            "total": 5,
            "sent": 3,
            "failed": 1,
            "started_at": 1.0,
            "finished_at": None,
            "last_error": "",
            "v": web_mod._BROADCAST_STATE_VERSION,
        }
        await db.set_setting(web_mod._BROADCAST_STATE_KEY, json.dumps(stored))

        was_running = await web_mod.recover_broadcast_state()
        snap = web_mod.broadcast_snapshot()
        assert was_running is True
        # Not left stuck in "running" (a new broadcast must not get a 409).
        assert snap["running"] is False
        assert snap["interrupted"] is True
        # Last known progress preserved, not reset to zero.
        assert (snap["total"], snap["sent"], snap["failed"]) == (5, 3, 1)
        assert snap["finished_at"] is not None
        assert snap["last_error"]

        # 2) A completed broadcast is restored verbatim (not "interrupted").
        stored.update(
            {
                "running": False,
                "sent": 5,
                "failed": 0,
                "finished_at": 2.0,
                "last_error": "",
            }
        )
        await db.set_setting(web_mod._BROADCAST_STATE_KEY, json.dumps(stored))
        assert await web_mod.recover_broadcast_state() is False
        snap = web_mod.broadcast_snapshot()
        assert snap["running"] is False and snap["interrupted"] is False
        assert (snap["total"], snap["sent"], snap["failed"]) == (5, 5, 0)

        # 3) Corrupt state must never crash startup.
        await db.set_setting(web_mod._BROADCAST_STATE_KEY, "{not json")
        assert await web_mod.recover_broadcast_state() is False
    finally:
        web_mod._broadcast_state.update(
            {
                "running": False,
                "total": 0,
                "sent": 0,
                "failed": 0,
                "started_at": None,
                "finished_at": None,
                "last_error": "",
                "interrupted": False,
            }
        )
        await db.close()


def test_broadcast_state_recovery_after_restart() -> None:
    asyncio.run(_recovery())
