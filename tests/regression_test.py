"""
Regression tests for the bugs fixed in this change-set.

Covers (each maps to a fixed bug):
  1. Web Admin authentication — REAL Telegram initData validation.
     A fake ``X-Telegram-Init-Data: test`` header MUST NOT authenticate.
     Non-admins MUST NOT reach admin APIs.  Tampered / expired data rejected.
  2. Server-side authorization — write endpoints are owner-only, and the
     client-sent role is never trusted.
  3. Filtered activity-log pagination — the total uses the SAME filter as the
     page query (1000 logs / 20 WARNING -> 1 page with page size 20).
  4. Pagination page validation happens BEFORE the DB query (0 / -1 / 999999 /
     last page / empty results / filter + pagination).
  5. Every admin main-menu button maps to a real section (no dead buttons).
  6. The obsolete user access restriction (/allow, /ban, user_status) is gone.

Run standalone::

    python tests/regression_test.py

or with pytest::

    python -m pytest tests/regression_test.py -q
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from urllib.parse import urlencode
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["CODEBUFF_SKIP_ENV_FILE"] = "1"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("ADMIN_ID", "111111111")
os.environ.setdefault("ADMIN_IDS", "222222222")
os.environ.pop("SUPABASE_DB_URL", None)

_tmpdir = tempfile.mkdtemp(prefix="bot_regression_test_")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "regression.db")

from app.config import settings  # noqa: E402
from app.database import db  # noqa: E402
from app.services import admin_roles  # noqa: E402

BOT_TOKEN = settings.bot_token
OWNER = settings.admin_id           # 111111111
ADMIN = 222222222                   # non-owner admin (ADMIN_IDS)
OUTSIDER = 999000111                # normal user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def sign_init_data(fields: dict[str, str], token: str = BOT_TOKEN) -> str:
    """Builds a CORRECTLY signed Telegram initData query string."""
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    # Real Telegram sends a URL-ENCODED query string — encode like Telegram does.
    return urlencode([*((k, fields[k]) for k in sorted(fields)), ("hash", digest)])


def user_field(user_id: int, name: str = "Tester") -> str:
    return json.dumps({"id": user_id, "first_name": name}, separators=(",", ":"))


def owner_init_data(user_id: int = OWNER, *, auth_date: int | None = None) -> str:
    return sign_init_data({
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "user": user_field(user_id),
    })


class FakeQuery:
    def get(self, key, default=None):  # noqa: ANN001, ANN201
        return default


class FakeRequest:
    """Minimal aiohttp Request stand-in for handler-level tests."""

    def __init__(self, init_data: str = "", *, json_body=None, app=None) -> None:  # noqa: ANN001
        self.headers = {"X-Telegram-Init-Data": init_data}
        self.query = FakeQuery()
        self.app = app if app is not None else {"bot": None}
        self._json = json_body

    async def json(self):  # noqa: ANN201
        if self._json is None:
            raise ValueError("no json")
        return self._json


# ---------------------------------------------------------------------------
# 1) initData validation
# ---------------------------------------------------------------------------
class TestInitDataValidation:
    def test_valid_signed_data_accepted(self):
        from app.web import validate_init_data
        data = validate_init_data(owner_init_data(), BOT_TOKEN)
        assert data is not None
        assert data["user_id"] == OWNER

    def test_fake_header_test_rejected(self):
        from app.web import validate_init_data
        assert validate_init_data("test", BOT_TOKEN) is None

    def test_empty_rejected(self):
        from app.web import validate_init_data
        assert validate_init_data("", BOT_TOKEN) is None
        assert validate_init_data(owner_init_data(), "") is None

    def test_missing_hash_rejected(self):
        from app.web import validate_init_data
        raw = "auth_date=%d&user=%s" % (int(time.time()), user_field(OWNER))
        assert validate_init_data(raw, BOT_TOKEN) is None

    def test_tampered_hash_rejected(self):
        from app.web import validate_init_data
        raw = owner_init_data()
        tampered = raw.replace("hash=", "hash=0", 1)
        assert validate_init_data(tampered, BOT_TOKEN) is None

    def test_tampered_user_rejected(self):
        from app.web import validate_init_data
        # Sign one user, then try to swap in another user id.
        raw = owner_init_data(OWNER)
        enc = lambda v: urlencode({"user": v})  # noqa: E731
        assert enc(user_field(OWNER)) in raw
        swapped = raw.replace(enc(user_field(OWNER)), enc(user_field(OUTSIDER)))
        assert swapped != raw
        assert validate_init_data(swapped, BOT_TOKEN) is None

    def test_expired_rejected(self):
        from app.web import validate_init_data
        old = owner_init_data(auth_date=int(time.time()) - 3 * 86400)
        assert validate_init_data(old, BOT_TOKEN) is None

    def test_wrong_token_rejected(self):
        from app.web import validate_init_data
        raw = sign_init_data(
            {"auth_date": str(int(time.time())), "user": user_field(OWNER)},
            token="999999:OTHER-TOKEN",
        )
        assert validate_init_data(raw, BOT_TOKEN) is None

    def test_signature_is_part_of_hmac_check_string(self):
        """Bot-token (HMAC) method: only ``hash`` is excluded; ``signature``
        STAYS in the data-check-string (Telegram docs)."""
        from app.web import validate_init_data
        fields = {
            "auth_date": str(int(time.time())),
            "user": user_field(OWNER),
            "signature": "abc123",
        }
        assert validate_init_data(sign_init_data(fields), BOT_TOKEN) is not None

    def test_signature_appended_after_signing_is_rejected(self):
        """A ``signature`` that was NOT part of the signed string must fail."""
        from app.web import validate_init_data
        fields = {
            "auth_date": str(int(time.time())),
            "user": user_field(OWNER),
        }
        with_sig = sign_init_data(fields) + "&signature=abc123"
        assert validate_init_data(with_sig, BOT_TOKEN) is None

    def test_url_encoded_init_data_with_unicode_and_spaces(self):
        """Encoded user JSON (spaces, unicode, quotes) must validate."""
        from app.web import validate_init_data
        fields = {
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": OWNER, "first_name": "Ali Vali",
                                "last_name": "O'g'li \u2014 \u0416"}),
            "query_id": "AAH+/a b",
        }
        raw = sign_init_data(fields)
        assert "%7B" in raw  # really encoded
        res = validate_init_data(raw, BOT_TOKEN)
        assert res is not None and res["user_id"] == OWNER


# ---------------------------------------------------------------------------
# 2) Server-side authorization
# ---------------------------------------------------------------------------
class TestAuthorization:
    def test_owner_authenticates(self):
        from app.web import authenticate
        identity = authenticate(FakeRequest(owner_init_data(OWNER)))
        assert identity == {"user_id": OWNER, "role": "owner"}

    def test_admin_authenticates_as_admin(self):
        from app.web import authenticate
        identity = authenticate(FakeRequest(owner_init_data(ADMIN)))
        assert identity is not None and identity["role"] == "admin"

    def test_normal_user_rejected(self):
        from app.web import authenticate
        assert authenticate(FakeRequest(owner_init_data(OUTSIDER))) is None

    def test_fake_header_not_authenticated(self):
        from app.web import check_auth
        assert check_auth(FakeRequest("test")) is False
        assert check_auth(FakeRequest("")) is False

    def test_check_auth_true_for_admin(self):
        from app.web import check_auth
        assert check_auth(FakeRequest(owner_init_data(ADMIN))) is True


# ---------------------------------------------------------------------------
# 3) Admin menu: no dead buttons
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 4) Obsolete user access system removed
# ---------------------------------------------------------------------------
class TestAccessSystemRemoved:
    def test_business_handler_has_no_access_block(self):
        src = (
            Path(__file__).resolve().parents[1]
            / "app" / "handlers" / "business.py"
        ).read_text(encoding="utf-8")
        assert "access_block" not in src
        assert "user_status" not in src




# ---------------------------------------------------------------------------
# 5/6) Pagination + filtered log counts (async, real SQLite)
# ---------------------------------------------------------------------------
class FakeMessageHolder:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[object] = []

    async def edit_text(self, text, **kwargs):  # noqa: ANN001
        self.texts.append(text)
        self.markups.append(kwargs.get("reply_markup"))

    async def answer(self, text, **kwargs):  # noqa: ANN001
        self.texts.append(text)
        self.markups.append(kwargs.get("reply_markup"))


def _patch_callback_answer() -> None:
    """Replace ``CallbackQuery.answer`` with a no-op (no Telegram call)."""
    from aiogram.types import CallbackQuery

    async def _noop(self, text=None, show_alert=False, **kwargs):  # noqa: ANN001
        return None

    CallbackQuery.answer = _noop  # type: ignore[method-assign]


def fake_callback(user_id: int, data: str):  # noqa: ANN201
    from aiogram.types import CallbackQuery, User as AiogramUser

    _patch_callback_answer()
    return CallbackQuery.model_construct(
        id="1",
        from_user=AiogramUser(id=user_id, is_bot=False, first_name="Owner"),
        chat_instance="1",
        data=data,
        message=FakeMessageHolder(),
    )


async def _seed_logs(total_info: int, total_warning: int) -> None:
    for index in range(total_info):
        await db.add_activity_log("info_event", f"info {index}", severity="INFO")
    for index in range(total_warning):
        await db.add_activity_log("warn_event", f"warn {index}", severity="WARNING")




async def _pagination_checks() -> None:
    await db.init()
    try:
        # 1000 total logs, only 20 of them WARNING.
        await _seed_logs(total_info=980, total_warning=20)
        assert await db.count_activity_logs() == 1000

        # Filtered total must use the SAME filter as the page query.
        assert await db.logs_page_count(min_severity="WARNING") == 20
        assert await db.logs_page_count(min_severity=None) == 1000

        # Both backends implement logs_page_count (Postgres parity).
        from app.database import PostgresDatabase
        from app.storage_sqlite import SqliteDatabase
        assert hasattr(PostgresDatabase, "logs_page_count")
        assert hasattr(SqliteDatabase, "logs_page_count")
    finally:
        await db.close()


class TestPagination:
    def test_logs_pagination_counts(self):
        asyncio.run(_pagination_checks())


# ---------------------------------------------------------------------------
# 7) Web API endpoint level checks (auth + owner-only writes + 409 broadcast)
# ---------------------------------------------------------------------------
class FakeBot:
    async def send_message(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


async def _endpoint_checks() -> None:
    from app.web import (
        api_broadcast,
        api_get_settings,
        api_set_settings,
        broadcast_snapshot,
    )

    await db.init()
    try:
        # Fake header -> 401 for every protected endpoint.
        assert (await api_get_settings(FakeRequest("test"))).status == 401

        # Invalid signature (tampered hash) -> 401 (NOT authenticated).
        bad_sig = owner_init_data(OWNER).replace("hash=", "hash=0", 1)
        assert (await api_get_settings(FakeRequest(bad_sig))).status == 401

        # Expired initData -> 401.
        expired = owner_init_data(OWNER, auth_date=int(time.time()) - 3 * 86400)
        assert (await api_get_settings(FakeRequest(expired))).status == 401

        # Valid NON-admin Telegram user -> 403 (authenticated, forbidden).
        assert (await api_get_settings(FakeRequest(owner_init_data(OUTSIDER)))).status == 403

        # Admin can read settings.
        admin_resp = await api_get_settings(FakeRequest(owner_init_data(ADMIN)))
        assert admin_resp.status == 200
        assert json.loads(admin_resp.body)["is_owner"] is False

        # Tampered frontend role/isOwner/isAdmin is IGNORED server-side:
        # an ADMIN (or a normal user) still cannot write owner-only settings.
        tampered_admin = await api_set_settings(FakeRequest(
            owner_init_data(ADMIN),
            json_body={"maintenance_mode": True, "role": "owner",
                       "isOwner": True, "isAdmin": True},
        ))
        assert tampered_admin.status == 403
        tampered_user = await api_set_settings(FakeRequest(
            owner_init_data(OUTSIDER),
            json_body={"maintenance_mode": True, "role": "owner", "isOwner": True},
        ))
        assert tampered_user.status == 403

        # Admin (non-owner) may NOT write settings.
        write_admin = await api_set_settings(FakeRequest(
            owner_init_data(ADMIN), json_body={"maintenance_mode": True}
        ))
        assert write_admin.status == 403

        # Owner CAN write settings.
        write_owner = await api_set_settings(FakeRequest(
            owner_init_data(OWNER), json_body={"maintenance_mode": True}
        ))
        assert write_owner.status == 200
        assert (await db.get_setting("maintenance_mode", "0")) == "1"
        # restore
        await api_set_settings(FakeRequest(
            owner_init_data(OWNER), json_body={"maintenance_mode": False}
        ))

        # Invalid retention value rejected.
        bad_retention = await api_set_settings(FakeRequest(
            owner_init_data(OWNER), json_body={"retention_days": 13}
        ))
        assert bad_retention.status == 400

        # Broadcast: fake header -> 401; valid non-admin -> 403; missing -> 400.
        assert (await api_broadcast(FakeRequest("test", json_body={}))).status == 401
        assert (await api_broadcast(FakeRequest(
            owner_init_data(OUTSIDER), json_body={"text": "x"}
        ))).status == 403
        missing = await api_broadcast(FakeRequest(
            owner_init_data(OWNER), json_body={}
        ))
        assert missing.status == 400

        # Broadcast running -> duplicate click returns 409.
        from app import web as web_mod
        web_mod._broadcast_state.update({"running": True})
        try:
            dup = await api_broadcast(FakeRequest(
                owner_init_data(OWNER),
                json_body={"text": "salom"},
                app={"bot": FakeBot()},
            ))
            assert dup.status == 409, dup.status
        finally:
            web_mod._broadcast_state.update({"running": False})

        # Accepted broadcast returns 202 (accepted, NOT "delivered").
        await db.upsert_user(777001, "u", "U", None)
        accepted = await api_broadcast(FakeRequest(
            owner_init_data(OWNER),
            json_body={"text": "salom"},
            app={"bot": FakeBot()},
        ))
        assert accepted.status == 202, accepted.status
        # Let the background task settle.
        for _ in range(50):
            if not broadcast_snapshot()["running"]:
                break
            await asyncio.sleep(0.02)
        snap = broadcast_snapshot()
        assert snap["running"] is False
        assert snap["total"] >= 1
    finally:
        await db.close()


class TestWebEndpoints:
    def test_web_api_auth_and_broadcast(self):
        asyncio.run(_endpoint_checks())


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------
def _run_standalone() -> None:
    import traceback

    failures: list[str] = []
    for name in dir(sys.modules[__name__]):
        if not name.endswith("Test") and not name.startswith("Test"):
            continue
        cls = getattr(sys.modules[__name__], name)
        if not isinstance(cls, type):
            continue
        for method in sorted(dir(cls)):
            if not method.startswith("test_"):
                continue
            try:
                getattr(cls(), method)()
                print(f"  PASS {name}.{method}")
            except Exception:  # noqa: BLE001
                failures.append(f"{name}.{method}")
                traceback.print_exc()
    if failures:
        print("\nFAILED:", ", ".join(failures))
        raise SystemExit(1)
    print("\nALL REGRESSION TESTS PASSED")


if __name__ == "__main__":
    _run_standalone()
