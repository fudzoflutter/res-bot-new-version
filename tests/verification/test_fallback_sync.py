"""SQLite fallback -> primary reconciliation verification (regression).

When Supabase/PostgreSQL is unreachable the bot keeps working on the local
SQLite file (``Database._init_sqlite(fallback=True)``).  Everything written
during that window must reach the primary database once it is available again —
without losing rows and without importing any row twice.

The production ``target`` is :class:`app.database.PostgresDatabase`, but the
reconciliation logic is deliberately backend-agnostic (``sync_cursors`` +
``apply_sync_batch``), so here we drive it with a second SQLite database that
stands in for the primary.  Both backends share the exact same algorithm.
"""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import contextmanager
from pathlib import Path

from app.config import settings
from app.services import fallback_sync
from app.storage_sqlite import SqliteDatabase


@contextmanager
def _db_path(path: Path):
    """Point ``SqliteDatabase.init()`` at *path* (frozen settings shim)."""
    old = settings.db_path
    object.__setattr__(settings, "db_path", str(path))
    try:
        yield
    finally:
        object.__setattr__(settings, "db_path", old)


async def _new_sqlite(path: Path) -> SqliteDatabase:
    with _db_path(path):
        backend = SqliteDatabase()
        await backend.init()
    return backend


async def _fallback_reconciliation() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fallback_sync_"))
    src_path = tmp / "fallback.db"
    dst_path = tmp / "primary.db"
    source = await _new_sqlite(src_path)
    target = await _new_sqlite(dst_path)
    try:
        # --- Writes made while running on the SQLite fallback --------------
        await source.upsert_user(7001, "alice", "Alice", None)
        await source.upsert_connection("bc_sync_1", 7001, True, 7001)
        await source.add_event(7001, "text", "hello", chat_id=9001)
        await source.add_event(
            7001, "edit", "hello v2", chat_id=9001, message_id=5
        )
        await source.add_activity_log(
            "connection_created",
            "created",
            connection_id="bc_sync_1",
            user_id=7001,
            severity="INFO",
        )
        await source.set_setting("retention_days", "30")

        # --- Primary becomes available again ------------------------------
        stats = await fallback_sync.sync_sqlite_into(target, path=str(src_path))
        assert stats["users"] == 1, stats
        assert stats["connections"] == 1, stats
        assert stats["events"] == 2, stats
        assert stats["activity_log"] == 1, stats
        assert stats["settings"] >= 1, stats

        assert await target.get_user(7001) is not None
        conn = await target.get_connection("bc_sync_1")
        assert conn is not None and conn.get("is_enabled")
        assert await target.count_events_multi(["text", "edit"]) == 2
        assert await target.count_activity_logs() == 1
        assert await target.get_setting("retention_days") == "30"

        # --- Idempotent: a second run imports nothing and duplicates nothing
        stats2 = await fallback_sync.sync_sqlite_into(target, path=str(src_path))
        assert stats2["events"] == 0 and stats2["activity_log"] == 0, stats2
        assert await target.count_events_multi(["text", "edit"]) == 2
        assert await target.count_activity_logs() == 1
        assert await target.count_users() == 1

        # --- New fallback writes after the first sync are still picked up --
        await source.add_event(7001, "delete", "gone", chat_id=9001)
        await source.add_activity_log("message_deleted", "deleted", user_id=7001)
        stats3 = await fallback_sync.sync_sqlite_into(target, path=str(src_path))
        assert stats3["events"] == 1 and stats3["activity_log"] == 1, stats3
        assert await target.count_events_multi(["delete"]) == 1
        assert await target.count_events_multi(["text", "edit"]) == 2  # no dup
        assert await target.count_activity_logs() == 2

        # --- A crash right after a batch can never duplicate on retry ------
        for _ in range(3):
            await fallback_sync.sync_sqlite_into(target, path=str(src_path))
        assert await target.count_activity_logs() == 2
        assert await target.count_events_multi(["text", "edit", "delete"]) == 3
    finally:
        await source.close()
        await target.close()


def test_fallback_reconciliation_no_loss_no_duplicates() -> None:
    asyncio.run(_fallback_reconciliation())


async def _high_water_seed() -> None:
    """The legacy one-shot import must not be duplicated by the new sync."""
    tmp = Path(tempfile.mkdtemp(prefix="fallback_seed_"))
    src_path = tmp / "fallback.db"
    dst_path = tmp / "primary.db"
    source = await _new_sqlite(src_path)
    target = await _new_sqlite(dst_path)
    try:
        # Source already has two events...
        await source.add_event(7001, "text", "old1", chat_id=9001)
        await source.add_event(7001, "text", "old2", chat_id=9001)
        # ...and the legacy import already copied them into the primary.
        await target.add_event(7001, "text", "old1", chat_id=9001)
        await target.add_event(7001, "text", "old2", chat_id=9001)
        assert await target.count_events_multi(["text"]) == 2

        # Seed cursors at the current high-water mark -> nothing re-imported.
        await fallback_sync.seed_cursors_at_high_water(target, path=str(src_path))
        stats = await fallback_sync.sync_sqlite_into(target, path=str(src_path))
        assert stats["events"] == 0, stats
        assert await target.count_events_multi(["text"]) == 2, "duplicated!"

        # Future writes are still reconciled.
        await source.add_event(7001, "text", "new3", chat_id=9001)
        stats = await fallback_sync.sync_sqlite_into(target, path=str(src_path))
        assert stats["events"] == 1, stats
        assert await target.count_events_multi(["text"]) == 3
    finally:
        await source.close()
        await target.close()


def test_fallback_seed_prevents_legacy_reimport() -> None:
    asyncio.run(_high_water_seed())


async def _recreated_file() -> None:
    """A wiped/recreated SQLite file must not be hidden by a stale cursor."""
    tmp = Path(tempfile.mkdtemp(prefix="fallback_gen_"))
    src_path = tmp / "fallback.db"
    dst_path = tmp / "primary.db"
    source = await _new_sqlite(src_path)
    target = await _new_sqlite(dst_path)
    try:
        await source.add_event(7001, "text", "gen1", chat_id=9001)
        await fallback_sync.sync_sqlite_into(target, path=str(src_path))
        assert await target.count_events_multi(["text"]) == 1

        # The file is deleted and recreated: ids/timestamps restart from 0.
        await source.close()
        src_path.unlink()
        fresh = await _new_sqlite(src_path)
        try:
            await fresh.add_event(7001, "text", "gen2", chat_id=9002)
            stats = await fallback_sync.sync_sqlite_into(target, path=str(src_path))
            assert stats["events"] == 1, stats
            assert await target.count_events_multi(["text"]) == 2
        finally:
            await fresh.close()
    finally:
        await target.close()
        await source.close()


def test_fallback_generation_detects_recreated_file() -> None:
    asyncio.run(_recreated_file())


def test_backends_expose_sync_interface() -> None:
    """Both storages must offer the reconciliation primitive."""
    from app.database import PostgresDatabase

    for backend in (SqliteDatabase, PostgresDatabase):
        assert hasattr(backend, "sync_cursors")
        assert hasattr(backend, "apply_sync_batch")
