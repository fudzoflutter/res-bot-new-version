"""pytest session bootstrap — freeze the shared test environment FIRST.

``app.config.settings`` is a module-level singleton built the first time
``app.config`` is imported.  Test files therefore all have to agree on ONE
env snapshot; otherwise the result depends on pytest's alphabetical import
order.  Setting the same values here guarantees a stable, leak-free run.

SAFETY: production credentials must NEVER be loaded by the test session.

  * ``CODEBUFF_SKIP_ENV_FILE=1`` — never read the real env.txt/.env.
  * ``SUPABASE_DB_URL`` is REMOVED from the environment before any import, so
    ``app.config`` sees an empty url and every backend-dependent test uses a
    throwaway SQLite file.  The value is stashed under an internal name for the
    one opt-in, read-only Supabase verification test (see
    ``tests/verification/test_supabase_readonly.py``).
  * ``DB_PATH`` — a fresh temp file, never the developer's real bot.db.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Stable, non-production test identities (mirrored by
# tests/verification/_verification_helpers.py).
os.environ.setdefault("CODEBUFF_SKIP_ENV_FILE", "1")
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("ADMIN_ID", "111111111")
os.environ.setdefault("ADMIN_IDS", "222222222")

# Take the live URL OUT of the environment so no normal test (and no
# app.config import) can ever reach production; keep it only for the explicit
# opt-in read-only check.
os.environ["PYTEST_STASHED_SUPABASE_URL"] = os.environ.pop("SUPABASE_DB_URL", "")

os.environ.setdefault(
    "DB_PATH", str(Path(tempfile.mkdtemp(prefix="bot_pytest_")) / "pytest.db")
)
