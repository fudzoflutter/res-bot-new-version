"""Deployment verification: healthcheck, readiness, startup safety and
staging/production configuration separation.

These are the checks Railway relies on:

* ``/health`` returns 503 until initialization finished, then 200 — with NO
  authentication, NO secrets and NO destructive work
* ``/ready`` additionally fails when the database stops answering
* a failed startup exits non-zero and is never reported healthy
* staging and production are separated purely by environment variables

Everything runs against a throwaway SQLite database and a local socket;
production credentials are never loaded (tests/conftest.py pins the session).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from _verification_helpers import FakeBot

from app import main as app_main
from app import web as web_mod
from app.config import settings
from app.database import db
from app.services import alerts
from app.web import start_web_server

_ROOT = Path(__file__).resolve().parents[2]


def _noop(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
    """Stand-in for side-effecting helpers (no logger/file setup in tests)."""
    return None


async def _health_over_http() -> None:
    import aiohttp

    await db.init()
    old_port = os.environ.get("PORT")
    os.environ["PORT"] = "0"  # let the OS pick a free port
    web_mod.mark_not_ready()
    try:
        site = await start_web_server(FakeBot())
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"

        async def get(path: str):
            async with aiohttp.ClientSession() as session:
                async with session.get(base + path) as resp:  # NO auth header
                    return resp.status, await resp.text()

        # --- before initialization: NOT healthy -----------------------------
        status, body = await get("/health")
        assert status == 503, status
        assert json.loads(body) == {"status": "starting"}, body
        status, body = await get("/ready")
        assert status == 503 and json.loads(body) == {"status": "starting"}

        # --- after initialization: healthy ----------------------------------
        web_mod.mark_ready()
        assert web_mod.is_ready() is True
        status, body = await get("/health")
        assert status == 200, status
        assert json.loads(body) == {"status": "ok"}, body
        status, body = await get("/ready")
        assert status == 200 and json.loads(body) == {"status": "ready"}, body

        # No secrets in the payloads (tokens, passwords, connection strings).
        for path in ("/health", "/ready"):
            _, payload = await get(path)
            assert settings.bot_token not in payload
            assert "postgresql" not in payload
            assert "password" not in payload.lower()

        # --- database unreachable -> readiness fails, liveness stays ok ------
        async def broken_ping() -> bool:
            return False

        original = db.ping
        db.ping = broken_ping  # type: ignore[method-assign]
        try:
            status, _ = await get("/ready")
            assert status == 503, "readiness must fail when the DB is down"
            assert (await get("/health"))[0] == 200, "liveness must stay independent"
        finally:
            db.ping = original  # type: ignore[method-assign]
    finally:
        web_mod.mark_not_ready()
        await site.stop()
        if old_port is None:
            os.environ.pop("PORT", None)
        else:
            os.environ["PORT"] = old_port
        await db.close()


def test_healthcheck_before_and_after_initialization() -> None:
    asyncio.run(_health_over_http())


def test_startup_failure_is_reported_and_never_healthy(monkeypatch) -> None:  # noqa: ANN001
    """A failing startup must exit non-zero and keep /health at 503."""

    async def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("database unreachable")

    class FakeRealBot:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            self.session = SimpleNamespace(close=_noop)

    monkeypatch.setattr(app_main, "setup_logging", _noop)
    monkeypatch.setattr(app_main, "Bot", FakeRealBot)
    monkeypatch.setattr(alerts, "configure_default_sender", _noop)
    monkeypatch.setattr(db, "init", boom)
    monkeypatch.setenv("PORT", "0")

    web_mod.mark_not_ready()
    try:
        assert app_main.run_cli() == 1, "a failed startup must exit non-zero"
        assert web_mod.is_ready() is False, "failed startup must not look healthy"
    finally:
        web_mod.mark_not_ready()


def test_clean_shutdown_exits_zero(monkeypatch) -> None:  # noqa: ANN001
    async def interrupted() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(app_main, "main", interrupted)
    assert app_main.run_cli() == 0


# ---------------------------------------------------------------------------
# Staging / production separation (purely environment driven)
# ---------------------------------------------------------------------------
def _settings_for(env: dict[str, str]) -> dict:
    """Run a fresh interpreter with *env* and dump the resolved settings."""
    child = os.environ.copy()
    for key in (
        "BOT_TOKEN",
        "ADMIN_ID",
        "ADMIN_IDS",
        "ADMIN_OWNER_IDS",
        "SUPABASE_DB_URL",
        "DB_PATH",
        "WEBAPP_URL",
        "ENVIRONMENT",
        "APP_ENV",
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_PROJECT_ID",
        "RENDER_SERVICE_ID",
        "DEBUG",
        "ENV_FILE",
    ):
        child.pop(key, None)
    child["CODEBUFF_SKIP_ENV_FILE"] = "1"  # never read env.txt/.env
    child.update(env)
    child["PYTHONPATH"] = str(_ROOT) + os.pathsep + child.get("PYTHONPATH", "")
    code = (
        "import json;"
        "from app.config import settings;"
        "print(json.dumps({"
        "'environment': settings.environment,"
        "'production': settings.production,"
        "'bot_token': settings.bot_token,"
        "'db_path': settings.db_path,"
        "'supabase_db_url': settings.supabase_db_url,"
        "'admin_id': settings.admin_id,"
        "}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=child,
        cwd=str(_ROOT),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_staging_and_production_are_separated_by_environment() -> None:
    staging = _settings_for(
        {
            "ENVIRONMENT": "staging",
            "BOT_TOKEN": "111:STAGING-TOKEN",
            "ADMIN_ID": "555000111",
            "DB_PATH": "staging.db",
            "SUPABASE_DB_URL": "postgresql://staging-user:staging-pass@staging.local:5432/postgres",
            "WEBAPP_URL": "https://staging.example.test/",
        }
    )
    production = _settings_for(
        {
            "ENVIRONMENT": "production",
            "BOT_TOKEN": "222:PRODUCTION-TOKEN",
            "ADMIN_ID": "777000222",
            "DB_PATH": "prod.db",
            "SUPABASE_DB_URL": "postgresql://prod-user:prod-pass@prod.local:5432/postgres",
            "WEBAPP_URL": "https://prod.example.test/",
        }
    )

    assert staging["environment"] == "staging"
    assert staging["production"] is False, "staging must never be flagged production"
    assert production["environment"] == "production"
    assert production["production"] is True

    # No cross-contamination: each environment gets its own credentials.
    for key in ("bot_token", "db_path", "supabase_db_url", "admin_id"):
        assert staging[key] != production[key], f"{key} leaked between environments"
    assert "staging" in staging["supabase_db_url"]
    assert "prod" in production["supabase_db_url"]


def test_environment_defaults_without_explicit_flag() -> None:
    """No ENVIRONMENT (a plain local run) is 'development', never production."""
    local = _settings_for({"BOT_TOKEN": "333:LOCAL-TOKEN", "ADMIN_ID": "999000333"})
    assert local["environment"] == "development"
    assert local["production"] is False
    assert local["supabase_db_url"] == ""  # nothing to fall back to by accident


def test_production_credentials_are_never_loaded_by_this_session() -> None:
    """The verification session itself must be pinned to test values."""
    assert os.environ.get("CODEBUFF_SKIP_ENV_FILE") == "1"
    assert settings.bot_token == "123456:TEST-TOKEN"
    assert settings.supabase_db_url == "", (
        "normal test runs must never see a real database URL"
    )
