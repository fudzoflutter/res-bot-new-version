"""Release-audit fixes: har bir tuzatilgan xato uchun regressiya testi.

Bu fayl audit hisobotidagi kritik/jiddiy xatolar qaytib kelmasligini
kafolatlaydi (broadcast, restart, hold-mode va h.k.).
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("BROADCAST_SEND_DELAY", "0")

from _verification_helpers import FakeBot  # noqa: E402

from app.database import db  # noqa: E402
from app.services import broadcast as engine  # noqa: E402
from app.services.broadcast import AdError, BroadcastBusy, build_ad, parse_buttons  # noqa: E402

_USERS = (8101, 8102, 8103, 8104, 8105)


def _reset_state() -> None:
    engine.state.clear()
    engine.state.update({
        "running": False, "total": 0, "sent": 0, "failed": 0, "pending": 0, "unknown": 0,
        "started_at": None, "finished_at": None, "last_error": "",
        "interrupted": False, "broadcast_id": "", "mode": "none", "requested_by": 0,
    })
    engine._starting = False  # noqa: SLF001


async def _wait_finished(timeout: float = 20.0) -> dict:
    for _ in range(int(timeout / 0.02)):
        if not engine.is_running():
            return engine.snapshot()
        await asyncio.sleep(0.02)
    raise AssertionError("broadcast tugamadi (timeout)")


# ---------------------------------------------------------------------------
# Broadcast: ikki parallel start() -> faqat BITTA broadcast
# ---------------------------------------------------------------------------
async def _parallel_start() -> None:
    await db.init()
    _reset_state()
    try:
        for uid in _USERS:
            await db.upsert_user(uid, f"u{uid}", "Ad", None)
        bot = FakeBot()
        ad = build_ad({"text": "reklama"})

        results = await asyncio.gather(
            engine.start(bot, ad, requested_by=1),
            engine.start(bot, ad, requested_by=1),
            return_exceptions=True,
        )
        ok = [r for r in results if isinstance(r, dict)]
        busy = [r for r in results if isinstance(r, BroadcastBusy)]
        assert len(ok) == 1 and len(busy) == 1, results

        await _wait_finished()
        for uid in _USERS:
            assert bot.sent.count(("message", uid)) == 1, f"{uid} ikki marta oldi"
    finally:
        _reset_state()
        await db.close()


def test_parallel_start_creates_single_broadcast() -> None:
    asyncio.run(_parallel_start())


# ---------------------------------------------------------------------------
# Broadcast: restartdan keyin PENDING qolganlar Retry orqali davom ettiriladi
# ---------------------------------------------------------------------------
async def _resume_pending() -> None:
    await db.init()
    _reset_state()
    try:
        bid = "bc_release_resume"
        await db.broadcast_seed(bid, list(_USERS))
        await db.broadcast_mark(bid, _USERS[0], "SENT")
        await db.broadcast_mark(bid, _USERS[1], "SENT")
        # _USERS[2..4] hech qachon urinilmagan (PENDING) — jarayon o'ldi.
        await engine.save_ad(bid, build_ad({"text": "reklama"}))
        import json
        await db.set_setting(engine.BROADCAST_STATE_KEY, json.dumps({
            "running": True, "total": 5, "sent": 2, "failed": 0,
            "broadcast_id": bid, "mode": "broadcast", "requested_by": 1,
            "v": engine.BROADCAST_STATE_VERSION,
        }))
        assert await engine.recover_state() is True

        bot = FakeBot()
        result = await engine.retry_failed(bot, requested_by=1)
        assert result["total"] == 3
        final = await _wait_finished()
        assert final["sent"] == 3 and final["failed"] == 0

        sent_ids = sorted(uid for _, uid in bot.sent)
        assert sent_ids == sorted(_USERS[2:]), "SENT qilinganlar takrorlanmasligi kerak"
        counts = await db.broadcast_counts(bid)
        assert counts["sent"] == 5 and counts["pending"] == 0 and counts["failed"] == 0

        # Endi hech narsa qolmadi -> AdError
        try:
            await engine.retry_failed(bot, requested_by=1)
        except AdError:
            pass
        else:  # pragma: no cover
            raise AssertionError("bo'sh retry AdError berishi kerak")
    finally:
        _reset_state()
        await db.close()


def test_interrupted_broadcast_resumes_pending_recipients() -> None:
    asyncio.run(_resume_pending())


# ---------------------------------------------------------------------------
# Broadcast: restartdan keyin SENDING -> UNKNOWN, Retry Failed dublikat bermaydi
# ---------------------------------------------------------------------------
async def _unknown_recipient_is_not_retried() -> None:
    await db.init()
    _reset_state()
    try:
        bid = "bc_release_unknown"
        uid = 8110
        await db.upsert_user(uid, "u8110", "Unknown", None)
        await db.broadcast_seed(bid, [uid])
        await db.broadcast_mark(bid, uid, "SENDING")
        await engine.save_ad(bid, build_ad({"text": "reklama"}))
        import json
        await db.set_setting(engine.BROADCAST_STATE_KEY, json.dumps({
            "running": True, "total": 1, "sent": 0, "failed": 0, "unknown": 0,
            "broadcast_id": bid, "mode": "broadcast", "requested_by": 1,
            "v": engine.BROADCAST_STATE_VERSION,
        }))

        assert await engine.recover_state() is True
        counts = await db.broadcast_counts(bid)
        assert counts["unknown"] == 1 and counts["failed"] == 0

        bot = FakeBot()
        try:
            await engine.retry_failed(bot, requested_by=1)
        except AdError:
            pass
        else:  # pragma: no cover
            raise AssertionError("UNKNOWN recipient retry qilinmasligi kerak")
        assert not bot.sent
    finally:
        _reset_state()
        await db.close()


def test_unknown_recipient_is_never_retried() -> None:
    asyncio.run(_unknown_recipient_is_not_retried())


# ---------------------------------------------------------------------------
# Tugma formati: defis bor matn / URL
# ---------------------------------------------------------------------------
def test_button_line_with_hyphens_parses() -> None:
    btns = parse_buttons("Bir-ikki - https://my-site.uz/a-b")
    assert btns[0].text == "Bir-ikki"
    assert btns[0].url == "https://my-site.uz/a-b"

    btns = parse_buttons(["Kanal - https://t.me/kanal", "Sayt-https://x.uz"])
    assert [b.text for b in btns] == ["Kanal", "Sayt"]
    assert btns[1].url == "https://x.uz"


# ---------------------------------------------------------------------------
# fallback_sync: eski bot.db PRIMARY'dagi yangiroq ma'lumotni bosib yozmaydi
# ---------------------------------------------------------------------------
async def _stale_sync_does_not_overwrite() -> None:
    import tempfile
    from pathlib import Path

    from app.services import fallback_sync
    from app.storage_sqlite import SqliteDatabase
    from app.config import settings

    tmp = Path(tempfile.mkdtemp(prefix="stale_sync_"))

    async def _new(path: Path) -> SqliteDatabase:
        old = settings.db_path
        object.__setattr__(settings, "db_path", str(path))
        try:
            backend = SqliteDatabase()
            await backend.init()
        finally:
            object.__setattr__(settings, "db_path", old)
        return backend

    source = await _new(tmp / "old.db")
    target = await _new(tmp / "primary.db")
    try:
        # Eski fayl: admin roli bor, ulanish yoqilgan, sozlama eski.
        await source.set_setting("admin_roles", '{"5": "admin"}')
        await source.set_setting("retention_days", "30")
        await source.upsert_connection("bc_stale", 9001, True, 9001)
        await source.upsert_user(9001, "old_name", "Old", None)
        # Sozlamalar/ulanish/user vaqtini ANIQ ESKI qilamiz.
        for sql in (
            "UPDATE bot_settings SET updated_at='2020-01-01T00:00:00'",
            "UPDATE connections SET connected_at='2020-01-01T00:00:00', disconnected_at=NULL",
            "UPDATE users SET last_activity='2020-01-01T00:00:00'",
        ):
            await source.conn.execute(sql)
        await source.conn.commit()

        # PRIMARY: rol bekor qilingan, ulanish o'chirilgan, sozlama yangilangan.
        await target.set_setting("admin_roles", "{}")
        await target.set_setting("retention_days", "90")
        await target.upsert_connection("bc_stale", 9001, True, 9001)
        await target.disable_connection("bc_stale") if hasattr(
            target, "disable_connection"
        ) else None
        await target.upsert_user(9001, "new_name", "New", None)

        await fallback_sync.sync_sqlite_into(target, path=str(tmp / "old.db"))
        # Ikkinchi start ham qayta yozmasligi kerak.
        await fallback_sync.sync_sqlite_into(target, path=str(tmp / "old.db"))

        assert await target.get_setting("admin_roles") == "{}"
        assert await target.get_setting("retention_days") == "90"
        user = await target.get_user(9001)
        assert user["username"] == "new_name"
    finally:
        await source.close()
        await target.close()


def test_stale_fallback_does_not_overwrite_newer_primary() -> None:
    asyncio.run(_stale_sync_does_not_overwrite())


# ---------------------------------------------------------------------------
# Middleware: runtime (panel orqali qo'shilgan) rol hold rejimida bloklanmaydi
# ---------------------------------------------------------------------------
def _message_update(user_id: int):  # noqa: ANN202
    from aiogram.types import Chat, Message as TgMessage, Update, User as TgUser

    user = TgUser(id=user_id, is_bot=False, first_name="U", username=f"u{user_id}")
    update = Update.model_construct(
        update_id=user_id,
        event_type="message",
        message=TgMessage.model_construct(
            message_id=1,
            chat=Chat(id=user_id, type="private"),
            from_user=user,
            text="/start",
        ),
    )
    return update, user


async def _runtime_role_bypasses_hold() -> None:
    from unittest.mock import patch

    from aiogram.types import Message as TgMessage

    from app.middlewares import RegisterUserMiddleware
    from app.services import admin_roles, maintenance
    from app.utils.tasks import drain

    runtime_mod, runtime_viewer, plain = 8801001, 8801002, 8801003
    await db.init()
    try:
        await admin_roles.add(runtime_mod, "moderator")
        await admin_roles.add(runtime_viewer, "viewer")
        assert admin_roles.is_admin(runtime_mod)
        await maintenance.set_enabled(True, notify_resume=False)

        mw = RegisterUserMiddleware()
        chained = {"n": 0}

        async def nxt(event, data):  # noqa: ANN001
            chained["n"] += 1
            return "ok"

        async def fake_answer(self, text=None, **kwargs):  # noqa: ANN001
            return None

        async def drive(uid: int) -> int:
            update, user = _message_update(uid)
            chained["n"] = 0
            await mw(nxt, update, {"event_from_user": user})
            return chained["n"]

        with patch.object(TgMessage, "answer", fake_answer):
            assert await drive(runtime_mod) == 1, "runtime moderator hold'da o'tishi shart"
            assert await drive(runtime_viewer) == 1
            assert await drive(plain) == 0, "oddiy foydalanuvchi hold'da to'xtaydi"
    finally:
        await drain()
        await maintenance.set_enabled(False, notify_resume=False)
        maintenance.reset_for_tests()
        admin_roles.reset_for_tests()
        await db.close()


def test_runtime_roles_bypass_hold_mode() -> None:
    asyncio.run(_runtime_role_bypasses_hold())


# ---------------------------------------------------------------------------
# Middleware: yangi foydalanuvchi monotonic() < 60 s bo'lganda ham yoziladi
# ---------------------------------------------------------------------------
async def _new_user_registered_even_if_monotonic_small() -> None:
    from unittest.mock import patch

    from app import middlewares
    from app.middlewares import RegisterUserMiddleware
    from app.utils.tasks import drain

    uid = 8802001
    await db.init()
    try:
        middlewares._last_upsert.pop(uid, None)  # noqa: SLF001
        mw = RegisterUserMiddleware()

        async def nxt(event, data):  # noqa: ANN001
            return "ok"

        update, user = _message_update(uid)
        # Yangi boot: monotonic() atigi 5 soniya.
        with patch.object(middlewares.time, "monotonic", return_value=5.0):
            await mw(nxt, update, {"event_from_user": user})
        await drain()
        assert await db.get_user(uid) is not None, "yangi foydalanuvchi yozilmadi"
    finally:
        await drain()
        await db.close()


def test_new_user_registered_with_small_monotonic_clock() -> None:
    asyncio.run(_new_user_registered_even_if_monotonic_small())
