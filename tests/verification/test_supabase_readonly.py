"""Read-only verification against a REAL Supabase/Postgres database.

Opt-in and READ-ONLY by design:

    ALLOW_SUPABASE_TESTS=1 SUPABASE_DB_URL=postgresql://... pytest -q

By default it is SKIPPED: ``tests/conftest.py`` removes ``SUPABASE_DB_URL``
from the environment so no normal test run can reach production.  This module
only reads (``SELECT``/``COUNT``) — it never issues DDL or DML, so it cannot
modify production data.
"""

from __future__ import annotations

import asyncio
import math
import os

import pytest

from app.config import settings
from app.database import PostgresDatabase, _severity_levels

_URL = os.environ.get("PYTEST_STASHED_SUPABASE_URL", "")
_OPT_IN = os.environ.get("ALLOW_SUPABASE_TESTS") == "1"

requires_live_db = pytest.mark.skipif(
    not (_OPT_IN and _URL),
    reason=(
        "read-only Supabase check — set ALLOW_SUPABASE_TESTS=1 and "
        "SUPABASE_DB_URL=... to enable"
    ),
)


async def _severity_counts(pool) -> dict[str, int]:  # noqa: ANN001
    async with pool.acquire() as conn:
        return {
            level: await conn.fetchval(
                "SELECT COUNT(*) FROM activity_log WHERE severity = ANY($1)",
                _severity_levels(level),
            )
            for level in ("INFO", "WARNING", "ERROR", "CRITICAL")
        }


async def _check() -> None:
    import asyncpg

    # Normal tests must never load production config.
    assert settings.supabase_db_url == "", "app.config loaded a live Supabase URL!"

    backend = PostgresDatabase()
    # Read-only pool: we deliberately never call init() (which runs idempotent
    # DDL) so this test cannot touch the schema at all.
    backend._pool = await asyncpg.create_pool(
        _URL, min_size=1, max_size=3, timeout=30, statement_cache_size=0
    )
    try:
        before = await _severity_counts(backend.pool)
        total = before["INFO"]  # INFO+ is the whole table (first severity level)

        # counts must match the same WHERE clause the page query uses
        assert await backend.logs_page_count() == total
        assert await backend.logs_page_count(min_severity="INFO") == before["INFO"]
        assert await backend.logs_page_count(min_severity="WARNING") == before["WARNING"]
        assert await backend.logs_page_count(min_severity="ERROR") == before["ERROR"]
        assert await backend.logs_page_count(min_severity="CRITICAL") == before["CRITICAL"]
        assert await backend.logs_page_count(min_severity="BOGUS") == total

        limit = 5  # page size used by the admin panel is 5

        # filtered pagination covers exactly the filtered set
        seen: list[dict] = []
        for page in range(math.ceil(before["WARNING"] / limit)):
            seen += await backend.logs_page(
                limit=limit, offset=page * limit, min_severity="WARNING"
            )
        assert len(seen) == before["WARNING"]
        assert len({row["id"] for row in seen}) == len(seen)
        assert all(row["severity"] != "INFO" for row in seen)

        # unfiltered pagination covers every row, newest first
        all_rows: list[dict] = []
        for page in range(math.ceil(total / limit)):
            all_rows += await backend.logs_page(limit=limit, offset=page * limit)
        assert len(all_rows) == total
        ids = [row["id"] for row in all_rows]
        assert ids == sorted(ids, reverse=True)

        # empty results
        assert await backend.logs_page(limit=10, offset=0, min_severity="CRITICAL") == []
        assert await backend.logs_page(limit=10, offset=10**9) == []
        assert (
            await backend.logs_page(limit=10, offset=10**9, min_severity="WARNING") == []
        )

        # nothing was written
        assert await _severity_counts(backend.pool) == before
    finally:
        await backend.pool.close()


def test_session_never_loads_production_credentials() -> None:
    """The pytest bootstrap must keep SUPABASE_DB_URL away from app.config.

    This runs in EVERY session (never skipped): no normal test may reach the
    production database.
    """
    assert "SUPABASE_DB_URL" not in os.environ
    assert settings.supabase_db_url == ""


@requires_live_db
def test_supabase_logs_pagination_is_readonly() -> None:
    asyncio.run(_check())
