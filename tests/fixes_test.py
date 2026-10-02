"""
Offline tests for the slim-bot cleanup batch.

Run from the project root:

    python tests/fixes_test.py

Covers (no Telegram network):

PART A — Cached-text management: the latest cached message is found,
         updated in place, and a fresh row wins over an older one.
PART B — Schema parity: only users / events / connections exist (no
         plans / payments / bot_settings) and events.sender_id is present.
PART C — Housekeeping: prunes keep the newest rows, counts stay correct.
PART D — Reporter regression: the daily limit / premium logic is GONE.
"""

from __future__ import annotations

import sys as _sys

# Windows konsolida emoji chop etish uchun (cp1252 UnicodeEncodeError bermasin).
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN"
os.environ["ADMIN_ID"] = "111111111"
os.environ["TEST_MODE"] = "0"  # Test mode off for tests
# env.txt real keys must never leak into tests (production Supabase!).
os.environ["CODEBUFF_SKIP_ENV_FILE"] = "1"

_tmpdir = tempfile.mkdtemp(prefix="bot_fixes_test_")
os.environ["DB_PATH"] = str(Path(_tmpdir) / "test.db")
os.environ.pop("SUPABASE_DB_URL", None)


async def part_a_cached_text() -> None:
    from app.database import db

    await db.init()
    try:
        uid = 93001
        await db.upsert_user(uid, "cacheuser", "Cache", None)
        await db.add_event(
            uid, "text", "original", chat_id=77, message_id=10, sender_id=uid
        )

        # Update in place (the reporter's edit flow).
        ok = await db.update_event_details(77, 10, "replaced")
        assert ok, "update_event_details must find the cached row"
        fresh = await db.get_event_by_message(77, 10)
        assert fresh["details"] == "replaced"

        # A newer row with the same (chat, message) wins.
        await db.add_event(
            uid, "text", "second", chat_id=77, message_id=10, sender_id=uid
        )
        assert (await db.get_event_by_message(77, 10))["details"] == "second"
        assert await db.update_event_details(77, 10, "latest")
        assert (await db.get_event_by_message(77, 10))["details"] == "latest"
        print("PART A (cached text) PASSED ✅")
    finally:
        await db.close()


async def part_b_schema_parity() -> None:
    from app.database import db

    await db.init()
    try:
        cursor = await db._backend.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in await cursor.fetchall()}
        await cursor.close()
        assert {"users", "events", "connections"} <= tables, tables
        # Admin panelning joriy jadvallari (bot_settings — retention /
        # maintenance, activity_log — faoliyat jurnali, instance_lock —
        # bir nusxa qulfi) MAJBURIY bo'lishi kerak.
        assert {"activity_log", "bot_settings", "instance_lock"} <= tables, tables
        for legacy in ("plans", "payments", "access"):
            assert legacy not in tables, f"legacy table {legacy} still created"

        cursor = await db._backend.conn.execute("PRAGMA table_info(events)")
        cols = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        assert "sender_id" in cols, "events.sender_id migration missing"
        print("PART B (schema parity) PASSED ✅")
    finally:
        await db.close()


async def part_c_housekeeping() -> None:
    from app.database import db

    await db.init()
    try:
        uid = 93003
        await db.upsert_user(uid, "pruneuser", "Prune", None)
        for i in range(10):
            await db.add_event(uid, "text", f"m{i}", chat_id=1, message_id=i,
                               sender_id=uid)
        assert await db.count_user_events(uid) == 10
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        assert await db.count_events(since=since) >= 10

        await db.prune_events(keep=3)
        assert await db.count_user_events(uid) == 3
        print("PART C (housekeeping) PASSED ✅")
    finally:
        await db.close()


def part_d_reporter_no_limit() -> None:
    from app.services import reporter as rep
    from app.services.reporter import Reporter

    assert not hasattr(Reporter, "_limit_reached"), "daily limit should be gone"
    assert not hasattr(Reporter, "_notify_limit_once")
    assert not hasattr(rep, "LIMIT_REACHED_USER", ), "premium limit text should be gone"
    # Premium/obuna va eski chat-admin panel modullari mavjud emas.
    for module in (
        "app.states",
        "app.filters",
        "app.services.broadcaster",
        "app.keyboards.access_kb",
        "app.keyboards.admin_kb",
        "app.handlers.admin_panel",
    ):
        try:
            __import__(module)
        except ModuleNotFoundError:
            continue
        raise AssertionError(f"{module} should have been removed")
    print("PART D (reporter regression) PASSED ✅")


async def main() -> None:
    await part_a_cached_text()
    await part_b_schema_parity()
    await part_c_housekeeping()
    part_d_reporter_no_limit()
    print("ALL FIXES TESTS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
