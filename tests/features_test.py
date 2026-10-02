"""
Offline feature tests for the slim bot.

Run from the project root:

    python tests/features_test.py

Covers (no Telegram network):

PART A — Connections: connect -> disconnect -> reconnect timestamps
PART B — Public counts: users / online counts + personal event counts
         (access is OPEN for everyone — no approval, no ban, no premium)
PART C — Link cleaner: strips tracking params, keeps clean links intact
PART D — Wiring: RegisterUserMiddleware exists; the admin/ban guard and
         premium APIs are GONE (regression guard against their return).
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN"
os.environ["ADMIN_ID"] = "111111111"
os.environ["TEST_MODE"] = "0"  # Test mode off for tests (all users allowed)
# env.txt loyihaning HAQIQIY kalitlarini o'z ichiga oladi (BOT_TOKEN,
# SUPABASE_DB_URL...).  Testlardan OLDIN uni yopamiz — aks holda app.config
# uni o'qib olib, testlar PRODUCTION Supabasega yozib yuboradi.
os.environ["CODEBUFF_SKIP_ENV_FILE"] = "1"

# Use the REAL sqlite backend in a temp file.
_tmpdir = tempfile.mkdtemp(prefix="bot_features_test_")
os.environ["DB_PATH"] = str(Path(_tmpdir) / "test.db")
os.environ.pop("SUPABASE_DB_URL", None)


async def part_a_connections() -> None:
    from app.database import db

    await db.init()
    try:
        await db.upsert_user(9001, "connuser", "Conn", None)
        await db.upsert_connection("bc_feat", 9001, True, user_chat_id=9001)
        conn = await db.get_connection("bc_feat")
        assert conn["is_enabled"] and conn["connected_at"]
        assert conn["disconnected_at"] is None

        await db.upsert_connection("bc_feat", 9001, False)
        conn = await db.get_connection("bc_feat")
        assert not conn["is_enabled"] and conn["disconnected_at"]

        await db.upsert_connection("bc_feat", 9001, True)
        conn = await db.get_connection("bc_feat")
        assert conn["is_enabled"] and conn["disconnected_at"] is None

        assert 9001 in await db.connected_user_ids()
        assert await db.connections_for_user(9001)
        print("PART A (connections) PASSED ✅")
    finally:
        await db.close()


async def part_b_counts() -> None:
    from app.database import db

    await db.init()
    try:
        # -- access is OPEN for everyone (no approval feature) ----------------
        await db.upsert_user(9002, "countuser", "Count", None)
        row = await db.get_user(9002)
        assert row is not None and row["username"] == "countuser"

        total = await db.count_users()
        online = await db.count_online()
        assert total >= 1 and online >= 1, (total, online)

        # -- personal event counts (statistics screen) ------------------------
        await db.add_event(9002, "text", "hi", chat_id=1, message_id=1, sender_id=9002)
        await db.add_event(9002, "edit", "old -> new", chat_id=1, message_id=1,
                           sender_id=9002)
        await db.add_event(9002, "delete_media", "file:x|photo|", chat_id=1,
                           message_id=2, sender_id=9002)
        assert await db.count_user_events(9002) == 3
        assert await db.count_user_events(9002, "edit") == 1
        assert await db.count_user_events(9002, "delete_media") == 1
        print("PART B (counts) PASSED ✅")
    finally:
        await db.close()


def part_c_link_cleaner() -> None:
    from app.services.linkcleaner import clean_text, clean_url, find_urls

    # -- tracking params are stripped -----------------------------------------
    assert clean_url(
        "https://shop.com/item?id=5&utm_source=telegram&utm_medium=social"
    ) == "https://shop.com/item?id=5"
    assert clean_url("https://x.com/a?fbclid=abc&gclid=def") == "https://x.com/a"
    assert clean_url("https://t.me/channel/12?si=ZxY") == "https://t.me/channel/12"

    # -- clean links are left untouched ---------------------------------------
    clean = "https://example.com/path?page=2#anchor"
    assert clean_url(clean) == clean

    # -- non-URL / bare domains are returned as-is ----------------------------
    assert clean_url("example.com?utm_source=x") == "example.com?utm_source=x"

    # -- find_urls + clean_text ------------------------------------------------
    text = "look https://a.com/x?utm_source=y and https://b.com/ok"
    urls = find_urls(text)
    assert urls == ["https://a.com/x?utm_source=y", "https://b.com/ok"], urls
    changed = clean_text(text)
    assert changed == [("https://a.com/x?utm_source=y", "https://a.com/x")], changed

    # -- no URLs -> no changes (silent) ---------------------------------------
    assert clean_text("just a sentence") == []
    assert clean_text(None) == []
    print("PART C (link cleaner) PASSED ✅")


def part_d_wiring() -> None:
    import importlib.util

    import app.keyboards  # noqa: F401
    import app.services.linkcleaner  # noqa: F401
    from app.middlewares import RegisterUserMiddleware

    assert RegisterUserMiddleware is not None

    # Admin/ban guard, premium screens and the OLD chat admin panel are GONE.
    import app.middlewares as middlewares

    assert not hasattr(middlewares, "AccessGuardMiddleware"), (
        "the admin/ban guard should have been removed"
    )
    assert not hasattr(middlewares, "invalidate_ban_cache")
    for stale in ("states", "filters"):
        assert stale not in dir(app), f"{stale} module should be removed"
    for gone in (
        "app.keyboards.admin_kb",
        "app.keyboards.access_kb",
        "app.handlers.admin_panel",
    ):
        assert importlib.util.find_spec(gone) is None, gone
    # /admin endi faqat Web Mini App tugmasini ochadi.
    assert importlib.util.find_spec("app.handlers.admin") is not None

    from app.database import Database

    for method in (
        "extend_premium", "set_premium", "create_plan", "create_payment",
        "set_banned", "set_admin", "access_rows", "set_access",
    ):
        assert not hasattr(Database, method), f"legacy API {method} should be gone"
    # Web panel sozlamalari (retention / maintenance) — JORIY.
    assert hasattr(Database, "get_setting") and hasattr(Database, "set_setting")
    print("PART D (wiring) PASSED ✅")


async def main() -> None:
    await part_a_connections()
    await part_b_counts()
    part_c_link_cleaner()
    part_d_wiring()
    print("ALL FEATURE TESTS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
