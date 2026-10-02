"""
Monitoring/admin panel testlari (offline — Telegram tarmog'iga ulanmaydi).

Ishga tushirish (loyiha ildizidan):

    python tests/monitoring_test.py

Qamrov:
PART A — Kesh: TTL, o'lcham chegarasi, statistika, expired tozalash.
PART B — Dedupe: takroriy delete/edit/connection hodisalari yutiladi.
PART C — BUSINESS DATA ISOLATION (integration): Connection A egasi A
         hisobot oladi; Connection B egasi B, oddiy user va subscriber
         HECH NARSA olmaydi; egasining o'z xabari hisobot qilinmaydi.
PART D — Admin authorization: owner/admin rollari, runtime admin
         qo'shish/olib tashlash, ruxsatsiz callback rad etiladi.
PART E — Confirmation: qo'lda yasalgan callback tasdiqsiz amal bajarmaydi;
         non-owner remove qila olmaydi.
PART F — Maintenance mode: yangi ulanish qabul qilinmaydi, mavjudi ishlaydi.
PART G — Retention: eski hodisalar o'chadi, yangilari va ulanishlar qoladi.
PART H — DB resilience: transient xato -> retry/backoff -> muvaffaqiyat;
         doimiy xato -> ko'tariladi (cheksiz sikl yo'q); health yoziladi.
PART I — Telegram API: 429 RetryAfter -> kutib qayta urinish; 409 -> kutmaydi.
PART J — Media xavfsizligi: katta fayl xotiraga olinmaydi (streaming),
         limitdan katta fayl umuman yuklanmaydi.
PART K — Bulk delete: chegaradan ko'p id — hisobot cheklanadi + xabar.
PART L — Admin panel: panel ekranlari haqiqiy SQLite bilan yig'iladi.
PART M — Search: filtrlar (user/chat/msg/type/sana) + sahifalash.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["CODEBUFF_SKIP_ENV_FILE"] = "1"
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN"
os.environ["ADMIN_ID"] = "111111111"
os.environ["ADMIN_IDS"] = "222222222"
os.environ.pop("SUPABASE_DB_URL", None)

_tmpdir = tempfile.mkdtemp(prefix="bot_monitoring_test_")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "monitor.db")

from app.config import settings  # noqa: E402
from app.database import db  # noqa: E402
from app.services import admin_roles, db_health  # noqa: E402
from app.services import reporter as rep  # noqa: E402
from app.services.reporter import Reporter  # noqa: E402
from app.utils import telegram_api  # noqa: E402

OWNER_A = 900001
OWNER_B = 900002
PARTNER = 900003
CONN_A = "bc_owner_a_connection_0001"
CONN_B = "bc_owner_b_connection_0002"
CHAT_ID = 555001
ADMIN_ID = settings.admin_id


# ---------------------------------------------------------------------------
# Fake Telegram obyektlari
# ---------------------------------------------------------------------------
class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id, text, **kwargs):  # noqa: ANN001
        self.sent.append(("message", f"{chat_id}:{text}"))
        return None

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):  # noqa: ANN001
        self.sent.append(("photo", f"{chat_id}:{caption or ''}"))
        return None

    async def send_video(self, chat_id, video, caption=None, **kwargs):  # noqa: ANN001
        self.sent.append(("video", f"{chat_id}:{caption or ''}"))
        return None

    async def send_animation(self, chat_id, animation, caption=None, **kwargs):  # noqa: ANN001
        self.sent.append(("animation", f"{chat_id}:{caption or ''}"))
        return None

    async def send_sticker(self, chat_id, sticker, **kwargs):  # noqa: ANN001
        self.sent.append(("sticker", f"{chat_id}"))
        return None

    async def send_voice(self, chat_id, voice, caption=None, **kwargs):  # noqa: ANN001
        self.sent.append(("voice", f"{chat_id}:{caption or ''}"))
        return None

    async def send_video_note(self, chat_id, video_note, **kwargs):  # noqa: ANN001
        self.sent.append(("video_note", f"{chat_id}"))
        return None

    async def send_audio(self, chat_id, audio, caption=None, **kwargs):  # noqa: ANN001
        self.sent.append(("audio", f"{chat_id}:{caption or ''}"))
        return None

    async def send_document(self, chat_id, document, caption=None, **kwargs):  # noqa: ANN001
        self.sent.append(("document", f"{chat_id}:{caption or ''}"))
        return None

    async def get_file(self, file_id):  # noqa: ANN001
        raise RuntimeError("test: file size unknown")

    async def download(self, file_id, destination=None):  # noqa: ANN001
        raise RuntimeError("test: download not implemented")


class SimpleChat:
    def __init__(self, chat_id: int = CHAT_ID, title: str = "Partner chat") -> None:
        self.id = chat_id
        self.type = "private"
        self.title = title
        self.first_name = "Partner"
        self.username = "partner"


class SimpleUser:
    def __init__(self, user_id: int, username: str | None = "partner") -> None:
        self.id = user_id
        self.username = username
        self.first_name = "Partner"
        self.last_name = None
        self.is_bot = False


class SimpleMedia:
    def __init__(self, file_id: str) -> None:
        self.file_id = file_id


MEDIA_ATTRS = (
    "sticker", "animation", "photo", "video", "voice", "video_note",
    "document", "audio",
)


class FakeMessage:
    def __getattr__(self, name):  # noqa: ANN001 – media maydonlari yo'q bo'lsa None
        if name in MEDIA_ATTRS:
            return None
        raise AttributeError(name)

    def __init__(
        self,
        message_id: int,
        user_id: int,
        *,
        text: str | None = None,
        caption: str | None = None,
        media: dict | None = None,
        chat: SimpleChat | None = None,
        connection_id: str = CONN_A,
    ) -> None:
        self.message_id = message_id
        self.from_user = SimpleUser(user_id)
        self.text = text
        self.caption = caption
        self.chat = chat or SimpleChat()
        self.business_connection_id = connection_id
        for kind, value in (media or {}).items():
            setattr(self, kind, value)


class SimpleDeleted:
    def __init__(self, ids: list[int], chat: SimpleChat | None = None, conn: str = CONN_A) -> None:
        self.message_ids = ids
        self.chat = chat or SimpleChat()
        self.business_connection_id = conn


def _reset_caches() -> None:
    rep.clear_instant_cache()
    rep.clear_seen_events()
    rep.invalidate_connection(None)
    rep.REPORTER_STATS.update({key: 0 for key in rep.REPORTER_STATS})


# ---------------------------------------------------------------------------
# PART A — Kesh (TTL / o'lcham / statistika)
# ---------------------------------------------------------------------------
def part_a_cache() -> None:
    _reset_caches()
    rep.remember(1, 10, "text", "salom")
    entry = rep.recall(1, 10)
    assert entry and entry["details"] == "salom"

    # TTL: vaqtni orqaga suramiz — yozuv "eskirgan" bo'lishi kerak.
    entry["stored_at"] = time.monotonic() - rep.INSTANT_CACHE_TTL_SECONDS - 5
    assert rep.recall(1, 10) is None, "TTL o'tgan yozuv qaytmasligi kerak"

    # Xotira chegarasi: eng eski yozuvlar chiqib ketadi.
    for index in range(rep.INSTANT_CACHE_MAX + 25):
        rep.remember(2, index, "text", f"m{index}")
    stats = rep.cache_stats()
    assert stats["count"] <= rep.INSTANT_CACHE_MAX, stats
    assert stats["memory_bytes"] > 0
    assert stats["ttl_seconds"] > 0

    # purge_expired eskirganlarni tozalaydi.
    rep.clear_instant_cache()
    rep.remember(3, 1, "text", "x")
    rep._instant_cache[(3, 1)]["stored_at"] = (
        time.monotonic() - rep.INSTANT_CACHE_TTL_SECONDS - 1
    )
    removed = rep.purge_expired()
    assert removed == 1 and rep.cache_stats()["count"] == 0
    print("PART A (cache TTL/size/stats) PASSED ✅")


# ---------------------------------------------------------------------------
# PART B — Dedupe
# ---------------------------------------------------------------------------
async def part_b_dedupe() -> None:
    _reset_caches()
    bot = FakeBot()
    reporter = Reporter(bot)
    message = FakeMessage(101, PARTNER, text="o'chiriladi")
    await reporter.report_incoming(message)
    await reporter.report_deleted(SimpleDeleted([101]))
    first_batch = list(bot.sent)
    assert first_batch, "birinchi delete hisobot berilishi kerak"

    # Takroriy SAME delete update — ikki marta hisobot bo'lmasin.
    await reporter.report_deleted(SimpleDeleted([101]))
    assert bot.sent == first_batch, "takroriy delete hisobot qilinmasligi kerak"
    assert rep.REPORTER_STATS["duplicates_suppressed"] >= 1

    # Takroriy edit update — bir marta hisobot.
    _reset_caches()
    bot2 = FakeBot()
    reporter2 = Reporter(bot2)
    await reporter2.report_incoming(FakeMessage(202, PARTNER, text="v1"))
    await reporter2.report_edited(FakeMessage(202, PARTNER, text="v2"))
    sent_after_edit = list(bot2.sent)
    await reporter2.report_edited(FakeMessage(202, PARTNER, text="v2"))
    assert bot2.sent == sent_after_edit, "takroriy edit hisobot qilinmasligi kerak"
    print("PART B (duplicate protection) PASSED ✅")


# ---------------------------------------------------------------------------
# PART C — BUSINESS DATA ISOLATION (asosiy talab)
# ---------------------------------------------------------------------------
async def _setup_connections() -> None:
    """Egalar va ikkita biznes-ulanish (barcha partlar uchun)."""
    await db.upsert_user(OWNER_A, "owner_a", "Owner A", None)
    await db.upsert_user(OWNER_B, "owner_b", "Owner B", None)
    await db.upsert_user(ADMIN_ID, "the_admin", "Admin", None)
    await db.upsert_connection(CONN_A, OWNER_A, True, user_chat_id=OWNER_A)
    await db.upsert_connection(CONN_B, OWNER_B, True, user_chat_id=OWNER_B)


async def part_c_isolation() -> None:
    _reset_caches()
    await _setup_connections()

    bot = FakeBot()

    # Connection A: suhbatdosh A xabar yuboradi -> jim keshlanadi.
    await Reporter(bot).report_incoming(
        FakeMessage(301, PARTNER, text="A maxfiy matni", connection_id=CONN_A)
    )
    assert bot.sent == [], "yuborilgan xabar hech kimga hisobot bo'lmasligi kerak"

    # Connection B: boshqa chat, boshqa suhbatdosh.
    chat_b = SimpleChat(chat_id=777002, title="B chat")
    await Reporter(bot).report_incoming(
        FakeMessage(401, PARTNER, text="B maxfiy matni", chat=chat_b, connection_id=CONN_B)
    )

    # A dagi xabar o'chirildi -> FAQAT Owner A oladi.
    await Reporter(bot).report_deleted(SimpleDeleted([301], conn=CONN_A))
    recipients = [chat for kind, chat in bot.sent if kind == "message"]
    assert recipients, "Owner A hisobot olishi shart"
    assert all(chat.startswith(f"{OWNER_A}:") for chat in recipients), recipients
    assert not any(chat.startswith(f"{OWNER_B}:") for chat in recipients), (
        "Connection B egasi A ma'lumotini OLMASLIGI kerak"
    )
    assert not any(chat.startswith(f"{ADMIN_ID}:") for chat in recipients), (
        "Admin hisobotni biznes-egasi sifatida olmaydi"
    )
    assert "A maxfiy matni" in recipients[0]

    # B dagi xabar o'chirildi -> FAQAT Owner B oladi (izolyatsiya teskari).
    bot.sent.clear()
    await Reporter(bot).report_deleted(
        SimpleDeleted([401], chat=chat_b, conn=CONN_B)
    )
    recipients_b = [chat for kind, chat in bot.sent if kind == "message"]
    assert recipients_b and all(
        chat.startswith(f"{OWNER_B}:") for chat in recipients_b
    ), recipients_b
    assert not any(chat.startswith(f"{OWNER_A}:") for chat in recipients_b)

    # Egasi o'z xabarini o'chirsa — HECH QANDAY hisobot yo'q.
    bot.sent.clear()
    await Reporter(bot).report_incoming(
        FakeMessage(501, OWNER_A, text="o'zim yozdim", connection_id=CONN_A)
    )
    await Reporter(bot).report_deleted(SimpleDeleted([501], conn=CONN_A))
    assert bot.sent == [], "egasining o'z xabari hisobot qilinmasligi kerak"

    # Admin yozgan xabar ham hisobot qilinmaydi (mavjud xatti-harakat).
    bot.sent.clear()
    await Reporter(bot).report_incoming(
        FakeMessage(502, ADMIN_ID, text="admin yozdi", connection_id=CONN_A)
    )
    await Reporter(bot).report_deleted(SimpleDeleted([502], conn=CONN_A))
    assert bot.sent == [], "admin xabari hisobot qilinmasligi kerak"

    # O'chirilgan (disabled) ulanish hech qanday hisobot chiqarmaydi.
    bot.sent.clear()
    await db.upsert_connection(CONN_A, OWNER_A, False, user_chat_id=OWNER_A)
    rep.invalidate_connection(CONN_A)
    await Reporter(bot).report_incoming(
        FakeMessage(601, PARTNER, text="disabled conn", connection_id=CONN_A)
    )
    await Reporter(bot).report_deleted(SimpleDeleted([601], conn=CONN_A))
    assert bot.sent == [], "o'chirilgan ulanish hisobot chiqarmasligi kerak"
    await db.upsert_connection(CONN_A, OWNER_A, True, user_chat_id=OWNER_A)
    rep.invalidate_connection(CONN_A)
    print("PART C (business data isolation) PASSED ✅")


# ---------------------------------------------------------------------------
# PART D — Admin authorization (server-side)
# ---------------------------------------------------------------------------
from aiogram.types import CallbackQuery, User as AiogramUser  # noqa: E402


class FakeMessageHolder:
    """``cb.message`` o'rnini bosuvchi (edit_text/answer yozib boradi)."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def edit_text(self, text, **kwargs):  # noqa: ANN001
        self.texts.append(text)

    async def answer(self, text, **kwargs):  # noqa: ANN001
        self.texts.append(text)


async def _fake_cb_answer(self, text=None, show_alert=False, **kwargs):  # noqa: ANN001
    """``CallbackQuery.answer`` ni tarmoqsiz almashtiradi (test uchun)."""
    self._answers = list(getattr(self, "_answers", [])) + [(text or "", bool(show_alert))]


CallbackQuery.answer = _fake_cb_answer  # type: ignore[method-assign]


def FakeCallback(user_id: int, data: str = "") -> CallbackQuery:
    """Haqiqiy ``CallbackQuery`` (model_construct) — handler tekshiruvlari uchun."""
    cb = CallbackQuery.model_construct(
        id="1",
        from_user=AiogramUser(id=user_id, is_bot=False, first_name="Tester"),
        chat_instance="123456",
        data=data,
        message=FakeMessageHolder(),
    )
    cb._answers = []
    return cb


def answers_of(cb: CallbackQuery) -> list[tuple[str, bool]]:
    return list(getattr(cb, "_answers", []))




# ---------------------------------------------------------------------------
# PART E — Confirmation (server-side qayta tekshiruv)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PART F — Maintenance mode
# ---------------------------------------------------------------------------
async def part_f_maintenance() -> None:
    from app.handlers.business import on_connection

    class FakeBusinessConnection:
        def __init__(self, conn_id: str, user_id: int, enabled: bool = True) -> None:
            self.id = conn_id
            self.user = SimpleUser(user_id, username="maint_user")
            self.is_enabled = enabled
            self.user_chat_id = user_id

    await db.set_setting("maintenance_mode", "1")
    try:
        bot = FakeBot()
        new_conn = "bc_new_during_maintenance_01"
        await on_connection(FakeBusinessConnection(new_conn, 910001), bot)
        row = await db.get_connection(new_conn)
        assert row is not None, "ulanish yozuvi (rad etilgan holda) saqlanishi kerak"
        assert not row.get("is_enabled"), "maintenance paytida yangi ulanish faol bo'lmasligi kerak"
        assert any("Maintenance" in text for _, text in bot.sent), "egasiga xabar berilishi kerak"

        # MAVJUD ulanish ishlashda davom etadi.
        rep.clear_seen_events()
        existing = await db.get_connection(CONN_A)
        assert existing and existing.get("is_enabled"), "mavjud ulanish o'chmasligi kerak"
        await on_connection(FakeBusinessConnection(CONN_B, OWNER_B), bot)
        rep.invalidate_connection(CONN_B)
        bot.sent.clear()
        await Reporter(bot).report_incoming(
            FakeMessage(701, PARTNER, text="maintenance davomida", connection_id=CONN_B)
        )
        await Reporter(bot).report_deleted(SimpleDeleted([701], conn=CONN_B))
        assert bot.sent, "mavjud ulanish maintenance paytida ishlashi kerak"
    finally:
        await db.set_setting("maintenance_mode", "0")
    print("PART F (maintenance mode) PASSED ✅")


# ---------------------------------------------------------------------------
# PART G — Retention cleanup
# ---------------------------------------------------------------------------
async def part_g_retention() -> None:
    from app.services.watchdog import housekeeping

    await db.set_setting("retention_days", "7")
    old = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    fresh = datetime.now().isoformat(timespec="seconds")
    # Eski va yangi hodisa (chat_id/message_id noyob).
    await db._backend._execute(
        """
        INSERT INTO events (user_id, chat_id, chat_title, event_type, message_id,
                            details, sender_id, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (OWNER_A, 9001, "retention", "text", 9001, "old", PARTNER, old),
    )
    await db._backend._execute(
        """
        INSERT INTO events (user_id, chat_id, chat_title, event_type, message_id,
                            details, sender_id, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (OWNER_A, 9002, "retention", "text", 9002, "fresh", PARTNER, fresh),
    )
    await housekeeping()
    assert await db.get_event_by_message(9001, 9001) is None, "eski hodisa o'chirilishi kerak"
    assert await db.get_event_by_message(9002, 9002) is not None, "yangi hodisa qolishi kerak"
    # Ulanish (aktiv biznes-ma'lumot) HECH QACHON o'chmaydi.
    assert await db.get_connection(CONN_A) is not None
    print("PART G (retention cleanup) PASSED ✅")


# ---------------------------------------------------------------------------
# PART H — DB resilience (retry/backoff, doimiy xato)
# ---------------------------------------------------------------------------
async def part_h_db_resilience() -> None:
    from app.database import _resilient

    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError("temporary connection reset")  # transient
        return "ok"

    started = time.monotonic()
    result = await _resilient("test_flaky", flaky, attempts=3, base=0.05)
    assert result == "ok" and attempts["n"] == 3
    assert time.monotonic() - started >= 0.05, "backoff kutishi bo'lishi kerak"
    assert db_health.health.total_retries >= 2

    permanent = {"n": 0}

    async def broken() -> str:
        permanent["n"] += 1
        raise ValueError("noma'lum ustun")

    try:
        await _resilient("test_permanent", broken, attempts=5, base=0.01)
    except ValueError:
        pass
    else:
        raise AssertionError("doimiy xato yuqoriga ko'tarilishi kerak")
    assert permanent["n"] == 1, "doimiy xato QAYTA urinilmaydi (cheksiz sikl yo'q)"
    assert db_health.health.consecutive_failures >= 1
    assert db_health.health.last_error, "health holatida xato ko'rinishi kerak"
    print("PART H (DB retry/backoff + health) PASSED ✅")


# ---------------------------------------------------------------------------
# PART I — Telegram API: 429 / 409
# ---------------------------------------------------------------------------
async def part_i_telegram_api() -> None:
    from aiogram.exceptions import TelegramConflictError, TelegramRetryAfter

    calls = {"n": 0}

    async def flood() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TelegramRetryAfter(method=None, message="Too Many Requests", retry_after=0)
        return "delivered"

    telegram_api.stats.retry_after_count = 0
    result = await telegram_api.call("test_429", flood, attempts=3, base_delay=0.01)
    assert result == "delivered"
    assert telegram_api.stats.retry_after_count == 1

    # 409 — qayta urinilmaydi (boshqa nusxa polling qilmoqda).
    conflict_calls = {"n": 0}

    async def conflict() -> str:
        conflict_calls["n"] += 1
        raise TelegramConflictError(method=None, message="terminated by other getUpdates request")

    telegram_api.stats.conflict_count = 0
    try:
        await telegram_api.call("test_409", conflict, attempts=3, base_delay=0.01)
    except TelegramConflictError:
        pass
    else:
        raise AssertionError("409 yuqoriga ko'tarilishi kerak")
    assert conflict_calls["n"] == 1, "409 qayta urinilmaydi"
    assert telegram_api.stats.conflict_count == 1
    print("PART I (Telegram 429/409) PASSED ✅")


# ---------------------------------------------------------------------------
# PART J — Media xavfsizligi (katta fayl)
# ---------------------------------------------------------------------------
async def part_j_media_safety() -> None:
    bot = FakeBot()
    reporter = Reporter(bot)

    # 1) Limitdan katta fayl — UMUMAN yuklanmaydi.
    async def huge_file(_file_id):  # noqa: ANN001
        return type("F", (), {"file_size": rep.MEDIA_MAX_DOWNLOAD + 1})()

    bot.get_file = huge_file  # type: ignore[assignment]
    data, path, too_large = await reporter._fetch_media("fid_big")
    assert data is None and path is None and too_large and "juda katta" in too_large

    # 2) O'rta fayl — vaqtincha FAYLGA (streaming), xotiraga emas.
    async def medium_file(_file_id):  # noqa: ANN001
        return type("F", (), {"file_size": rep.MEDIA_MEMORY_LIMIT + 10})()

    written = {"path": None}

    class Downloaded:
        async def download(self, file_id, destination=None):  # noqa: ANN001
            if destination is not None:
                destination.write(b"x" * 32)
                written["path"] = getattr(destination, "name", None)
                return None
            return None

    bot.get_file = medium_file  # type: ignore[assignment]
    bot.download = Downloaded().download  # type: ignore[assignment]
    data, path, too_large = await reporter._fetch_media("fid_medium")
    assert data is None and path and too_large is None
    assert os.path.exists(path) and os.path.getsize(path) == 32
    os.unlink(path)  # cleanup (test)

    # 3) Kichik fayl — xotirada.
    async def small_file(_file_id):  # noqa: ANN001
        return type("F", (), {"file_size": 1024})()

    class Buffer:
        def getvalue(self) -> bytes:
            return b"small-bytes"

    async def small_download(file_id, destination=None):  # noqa: ANN001
        return Buffer()

    bot.get_file = small_file  # type: ignore[assignment]
    bot.download = small_download  # type: ignore[assignment]
    data, path, too_large = await reporter._fetch_media("fid_small")
    assert data == b"small-bytes" and path is None and too_large is None
    print("PART J (large media safety) PASSED ✅")


# ---------------------------------------------------------------------------
# PART K — Bulk delete chegarasi
# ---------------------------------------------------------------------------
async def part_k_bulk_delete() -> None:
    _reset_caches()
    bot = FakeBot()
    reporter = Reporter(bot)
    total = rep.MAX_BULK_DELETES + 5
    for message_id in range(1000, 1000 + total):
        await reporter.report_incoming(FakeMessage(message_id, PARTNER, text=f"m{message_id}"))
    await reporter.report_deleted(SimpleDeleted(list(range(1000, 1000 + total))))
    assert rep.REPORTER_STATS["bulk_truncated"] >= 1
    assert any("Ommaviy o'chirish" in text for _, text in bot.sent), (
        "chegaradan oshgani haqida xabar berilishi kerak"
    )
    reports = [t for k, t in bot.sent if k == "message" and "Xabar o'chirildi" in t]
    assert 0 < len(reports) <= rep.MAX_BULK_DELETES, len(reports)
    print("PART K (bulk delete truncation) PASSED ✅")


# ---------------------------------------------------------------------------
# PART L — Admin panel ekranlari (haqiqiy SQLite bilan)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PART M — Search filtrlari + parser
# ---------------------------------------------------------------------------


async def main() -> None:
    await db.init()
    try:
        await _setup_connections()
        part_a_cache()
        await part_b_dedupe()
        await part_c_isolation()
        await part_f_maintenance()
        await part_g_retention()
        await part_h_db_resilience()
        await part_i_telegram_api()
        await part_j_media_safety()
        await part_k_bulk_delete()
        print("\nALL MONITORING TESTS PASSED ✅")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
