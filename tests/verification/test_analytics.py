"""Analytics verification — HAQIQIY baza ma'lumotlari, oraliqlar va validatsiya.

Tekshiriladi:

* barcha ko'rsatkichlar bazadan (hardcode/demo/tasodifiy son YO'Q);
* 24 soat -> SOATLI, 7/30 kun -> KUNLIK agregatsiya;
* custom sana oralig'i (validatsiya + parametrlangan so'rov);
* vaqt mintaqasi bazadagi yozuvlar bilan izchil (aralash aware/naive yo'q);
* takroriy Telegram update ikki marta SANALMAYDI;
* ruxsatlar: viewer faqat o'qish (analytics.view), boshqasi 403.

Barchasi thrash SQLite bazasida ishlaydi; production credentials yuklanmaydi.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from _verification_helpers import (
    ADMIN_ID,
    OUTSIDER,
    OWNER,
    Deleted,
    IncomingMessage,
    make_callback,
    make_init_data,
)

from app.database import db
from app.handlers import business as bh
from app.utils.timeutils import local_now
from app.services import analytics, moderation
from app.services import permissions as perms
from app.services import reporter as rep
from app.web import api_analytics

# Boshqa verification modullari bilan to'qnashmaslik uchun noyob ID fazosi.
_U = 880_000
VIEWER = _U + 1

#: Noyob chatlar (events jadvalidagi yozuvlar aralashmasligi uchun).
_CHAT = 880_500


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


async def _insert_event(
    *,
    user_id: int,
    event_type: str,
    occurred_at: datetime,
    chat_id: int = _CHAT,
    message_id: int = 1,
) -> None:
    """Vaqti ANIQ ko'rsatilgan hodisa (test uchun to'g'ridan-to'g'ri INSERT)."""
    await db._backend._execute(
        """
        INSERT INTO events (user_id, chat_id, chat_title, event_type,
                            message_id, details, sender_id, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id, chat_id, "analytics", event_type, message_id,
            "fixture", user_id, _iso(occurred_at),
        ),
    )


async def _insert_user(user_id: int, *, last_activity: datetime | None) -> None:
    await db._backend._execute(
        """
        INSERT INTO users (user_id, username, first_name, last_name,
                           connected_at, last_activity, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id, f"u{user_id}", "A", None, _iso(datetime.now()),
            _iso(last_activity) if last_activity else None,
            _iso(datetime.now() - timedelta(days=60)),
        ),
    )


async def _baseline() -> dict:
    now = datetime.now()
    from app.services import analytics as _a

    window = _a.resolve_range("24h", now=now)
    return await db.analytics_overview(_iso(window["since"]), _iso(window["until"]))


# ---------------------------------------------------------------------------
# 1) Dashboard ko'rsatkichlari (delta bo'yicha — global baza ham to'g'ri)
# ---------------------------------------------------------------------------
async def _overview() -> None:
    await db.init()
    try:
        before = await _baseline()
        now = datetime.now()
        fresh_user, stale_user = _U + 10, _U + 11
        await _insert_user(fresh_user, last_activity=now)
        await _insert_user(stale_user, last_activity=now - timedelta(days=10))
        await db.upsert_connection(f"bc_an_{_U}_a", fresh_user, True, fresh_user)
        await db.upsert_connection(f"bc_an_{_U}_b", stale_user, False, stale_user)

        # 24 soat ichida: 3 xabar, 1 tahrir, 1 o'chirish, 1 media.
        for index in range(3):
            await _insert_event(
                user_id=fresh_user, event_type="text",
                occurred_at=now - timedelta(minutes=index + 1), message_id=10 + index,
            )
        await _insert_event(user_id=fresh_user, event_type="edit", occurred_at=now - timedelta(minutes=5), message_id=20)
        await _insert_event(user_id=fresh_user, event_type="delete", occurred_at=now - timedelta(minutes=6), message_id=21)
        await _insert_event(user_id=fresh_user, event_type="photo", occurred_at=now - timedelta(minutes=7), message_id=22)
        # Oynadan TASHQARI hodisa (2 kun oldin) — sanalmasligi kerak.
        await _insert_event(user_id=fresh_user, event_type="text", occurred_at=now - timedelta(days=2), message_id=30)

        after = await _baseline()
        assert after["users_total"] - before["users_total"] == 2
        assert after["users_active"] - before["users_active"] == 1  # faqat fresh_user
        assert after["connections_total"] - before["connections_total"] == 2
        assert after["connections_active"] - before["connections_active"] == 1
        # "messages" = edit/delete/delete_media/connection BO'LMAGAN hodisalar:
        # 3 matn + 1 media (media ham QABUL QILINGAN xabar hisoblanadi).
        assert after["messages"] - before["messages"] == 4
        assert after["edited"] - before["edited"] == 1
        assert after["deleted"] - before["deleted"] == 1
        assert after["media"] - before["media"] == 1

        # Banned users ko'rsatkichi ban ro'yxatidan olinadi.
        payload = await analytics.overview(
            now - timedelta(days=1), now
        )
        assert payload["users_banned"] == moderation.count_banned()
        await moderation.ban(stale_user, by=OWNER, reason="analytics test")
        payload = await analytics.overview(now - timedelta(days=1), now)
        assert payload["users_banned"] == moderation.count_banned() + 0
        await moderation.unban(stale_user)
        assert (await analytics.dashboard("24h", now=now))["metrics"]["users_banned"] == moderation.count_banned()
    finally:
        await db.close()


def test_analytics_overview_metrics() -> None:
    asyncio.run(_overview())


# ---------------------------------------------------------------------------
# 2) Agregatsiya: soatli (24h) va kunlik (7/30 kun)
# ---------------------------------------------------------------------------
async def _aggregation() -> None:
    """Deterministik: "hozir" PIN qilingan vaqt — boshqa testlar ta'sir qilmaydi."""
    await db.init()
    pinned = datetime(2026, 6, 15, 12, 0, 0)
    try:
        user_id = _U + 20
        await _insert_user(user_id, last_activity=pinned)
        # 24 soat ichida: pinned vaqtda 4 ta, 3 soat oldin 2 ta.
        for index in range(4):
            await _insert_event(
                user_id=user_id, event_type="text",
                occurred_at=pinned, chat_id=_CHAT + 1, message_id=100 + index,
            )
        for index in range(2):
            await _insert_event(
                user_id=user_id, event_type="text",
                occurred_at=pinned - timedelta(hours=3),
                chat_id=_CHAT + 1, message_id=200 + index,
            )
        # 7/30 kunlik oynalar uchun: 3 kun va 10 kun oldin.
        await _insert_event(user_id=user_id, event_type="text", occurred_at=pinned - timedelta(days=3), chat_id=_CHAT + 2, message_id=301)
        await _insert_event(user_id=user_id, event_type="text", occurred_at=pinned - timedelta(days=10), chat_id=_CHAT + 2, message_id=302)
        # 30 kunlik oynadan TASHQARIDA (40 kun oldin).
        await _insert_event(user_id=user_id, event_type="text", occurred_at=pinned - timedelta(days=40), chat_id=_CHAT + 2, message_id=303)

        day = await analytics.dashboard("24h", now=pinned)
        assert day["bucket"] == "hour", day["bucket"]
        labels = [point["bucket"] for point in day["series"]]
        assert all(len(label) == 13 for label in labels), labels[:3]  # YYYY-MM-DDTHH
        assert len(labels) == 25, len(labels)  # 24 soat + joriy soat
        assert labels == sorted(labels), "soatli bucket'lar tartibda bo'lishi kerak"
        values = {point["bucket"]: point["messages"] for point in day["series"]}
        assert values[pinned.strftime("%Y-%m-%dT%H")] == 4
        assert values[(pinned - timedelta(hours=3)).strftime("%Y-%m-%dT%H")] == 2
        assert day["metrics"]["messages"] == 6
        assert sum(point["messages"] for point in day["series"]) == 6

        week = await analytics.dashboard("7d", now=pinned)
        assert week["bucket"] == "day"
        assert all(len(point["bucket"]) == 10 for point in week["series"])
        assert len(week["series"]) == 8, len(week["series"])
        assert week["metrics"]["messages"] == 7  # 6 + 3 kun oldingi
        assert sum(point["messages"] for point in week["series"]) == 7

        month = await analytics.dashboard("30d", now=pinned)
        assert month["bucket"] == "day"
        assert 30 <= len(month["series"]) <= 31, len(month["series"])
        # 30 kunlik oyna: 6 + 3 kun + 10 kun = 8; 40 kun oldingi hodisa KIRMAYDI.
        assert month["metrics"]["messages"] == 8
        assert sum(point["messages"] for point in month["series"]) == 8

        # 40 kun oldingi hodisa o'z oynasida aniq ko'rinadi (yo'qolmagan).
        window = analytics.resolve_range("custom", "2026-05-05", "2026-05-08")
        data = await db.analytics_overview(_iso(window["since"]), _iso(window["until"]))
        assert data["messages"] == 1
    finally:
        await db.close()


def test_analytics_aggregation_buckets() -> None:
    asyncio.run(_aggregation())


# ---------------------------------------------------------------------------
# 3) Custom oraliq + validatsiya
# ---------------------------------------------------------------------------
async def _custom_range() -> None:
    await db.init()
    try:
        # Aniq o'tmishdagi hodisalar — deterministik tekshiruv uchun.
        start = datetime(2026, 1, 10, 8, 0, 0)
        user_id = _U + 30
        await _insert_user(user_id, last_activity=start)
        await _insert_event(user_id=user_id, event_type="text", occurred_at=start, chat_id=_CHAT + 3, message_id=401)
        await _insert_event(user_id=user_id, event_type="text", occurred_at=start + timedelta(hours=1), chat_id=_CHAT + 3, message_id=402)
        await _insert_event(user_id=user_id, event_type="edit", occurred_at=start + timedelta(hours=2), chat_id=_CHAT + 3, message_id=403)
        await _insert_event(user_id=user_id, event_type="video", occurred_at=start + timedelta(hours=2), chat_id=_CHAT + 3, message_id=404)
        # Oraliqdan TASHQARI (keyingi kun).
        await _insert_event(user_id=user_id, event_type="text", occurred_at=start + timedelta(days=3), chat_id=_CHAT + 3, message_id=405)

        window = analytics.resolve_range("custom", "2026-01-10", "2026-01-11")
        assert window["bucket"] == "hour"  # 2 kunlik oraliq -> soatli
        data = await db.analytics_overview(
            _iso(window["since"]), _iso(window["until"])
        )
        # 2 matn + 1 video qabul qilingan xabar; 1 tahrir alohida hisoblanadi.
        assert data["messages"] == 3
        assert data["edited"] == 1
        assert data["media"] == 1

        payload = await analytics.dashboard("custom", "2026-01-10", "2026-01-11")
        assert payload["range"] == "custom"
        assert payload["metrics"]["messages"] == 3
        assert len(payload["series"]) == 48  # 10-11 yanvar, soatlik bucket'lar
        assert sum(point["messages"] for point in payload["series"]) == 3
        assert payload["series"][0]["bucket"] == "2026-01-10T00"

        # --- validatsiya ---------------------------------------------------
        with pytest.raises(analytics.AnalyticsError):
            analytics.resolve_range("yesterday")
        with pytest.raises(analytics.AnalyticsError):
            analytics.resolve_range("custom")
        with pytest.raises(analytics.AnalyticsError):
            analytics.resolve_range("custom", "2026-02-01", "2026-01-01")
        with pytest.raises(analytics.AnalyticsError):
            analytics.resolve_range("custom", "2026-01-01", "2099-01-01")  # kelajak
        with pytest.raises(analytics.AnalyticsError):
            analytics.resolve_range("custom", "2020-01-01", "2026-01-01")  # juda uzun
        with pytest.raises(analytics.AnalyticsError):
            analytics.resolve_range("custom", "10.01.2026", "2026-01-11")

        # "Bugun" tugash sanasi sifatida RUXSAT ETILADI (kun oxiri hozirgi
        # paytgacha qisqartiriladi) — aks holda bugungi oraliqni tanlab
        # bo'lmasdi.  Kelasi kun esa baribir rad etiladi.  (Vaqt PIN qilinadi:
        # natija yarim tunda ham bir xil bo'lishi uchun.)
        pinned = datetime(2026, 5, 20, 15, 30, 0)
        window_today = analytics.resolve_range(
            "custom", "2026-05-20", "2026-05-20", now=pinned
        )
        assert window_today["until"] == pinned
        assert window_today["since"].date() == window_today["until"].date()
        with pytest.raises(analytics.AnalyticsError):
            analytics.resolve_range("custom", "2026-05-20", "2026-05-21", now=pinned)
        # Injection urinishi — validatsiya rad etadi va so'rov bajarilmaydi.
        with pytest.raises(analytics.AnalyticsError):
            analytics.resolve_range(
                "custom", "2026-01-01'; DROP TABLE events; --", "2026-01-11"
            )
        assert await db.count_events() >= 4  # jadval joyida
        assert await _event_table_exists() is True
    finally:
        await db.close()


async def _event_table_exists() -> bool:
    row = await db._backend._fetch_one("SELECT COUNT(*) AS n FROM events")
    return bool(row and int(row["n"]) >= 0)


def test_analytics_custom_range_and_validation() -> None:
    asyncio.run(_custom_range())


def test_range_timezone_is_consistent_with_storage() -> None:
    """Chegaralar bazadagi naive-mahalliy format bilan bir xil bo'lishi shart."""
    window = analytics.resolve_range("24h")
    since, until = window["since"], window["until"]
    assert since.tzinfo is None and until.tzinfo is None, "naive (mahalliy) bo'lishi kerak"
    assert since < until
    # Baza va chegaralar HISOBOT MINTAQASIDA (Toshkent) — server soatida emas.
    assert abs((local_now() - until).total_seconds()) < 5
    stored = _iso(local_now())
    assert _iso(since) <= stored <= _iso(until)  # leksikografik taqqoslash
    assert "+" not in _iso(since) and "+" not in _iso(until)


# ---------------------------------------------------------------------------
# 4) Takroriy Telegram update ikki marta sanalmaydi
# ---------------------------------------------------------------------------
async def _dedupe() -> None:
    await db.init()
    try:
        rep.clear_instant_cache()
        rep.clear_seen_events()
        owner = _U + 40
        conn = f"bc_an_{_U}_dedupe"
        await db.upsert_connection(conn, owner, True, owner)

        from _verification_helpers import FakeBot

        # Birinchi qabul (kescha yoziladi) va keyin TAHRIR ikki marta.
        await bh.on_business_message(
            IncomingMessage(501, owner, text="salom", connection_id=conn), FakeBot()
        )
        await bh.on_edited(
            IncomingMessage(501, owner, text="salom v2", connection_id=conn), FakeBot()
        )

        now = datetime.now()
        counted = (await analytics.dashboard("24h", now=now))["metrics"]["edited"]
        # Aynan bir xil tahrir yana keladi (Telegram duplicate update).
        await bh.on_edited(
            IncomingMessage(501, owner, text="salom v2", connection_id=conn), FakeBot()
        )
        again = (await analytics.dashboard("24h", now=now))["metrics"]["edited"]
        assert again == counted, "takroriy update ikki marta sanaldi"

        # O'chirish ham bir marta sanaladi.
        await bh.on_deleted(Deleted([501], connection_id=conn), FakeBot())
        after_delete = (await analytics.dashboard("24h", now=now))["metrics"]["deleted"]
        await bh.on_deleted(Deleted([501], connection_id=conn), FakeBot())
        assert (await analytics.dashboard("24h", now=now))["metrics"]["deleted"] == after_delete
        rep.clear_seen_events()
        rep.clear_instant_cache()
    finally:
        await db.close()


def test_duplicate_updates_are_not_double_counted() -> None:
    asyncio.run(_dedupe())


# ---------------------------------------------------------------------------
# 5) Panel va Web endpoint (ruxsat + bogus oraliq)
# ---------------------------------------------------------------------------


def _fsm(user_id: int):  # noqa: ANN201
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


async def _endpoints() -> None:
    from app.services import admin_roles

    await db.init()
    try:
        await admin_roles.load()
        await admin_roles.add(VIEWER, perms.ROLE_VIEWER)

        class _Req:
            def __init__(self, user_id: int, query: dict | None = None) -> None:
                self.headers = {"X-Telegram-Init-Data": make_init_data(user_id)}
                params = dict(query or {})
                self.query = SimpleNamespace(
                    get=lambda key, default=None: params.get(key, default)
                )
                self.app = {"bot": None}

        ok = await api_analytics(_Req(VIEWER))
        assert ok.status == 200
        payload = __import__("json").loads(ok.body)
        assert payload["role"] == "viewer"
        assert payload["metrics"]["users_total"] >= 1
        assert payload["series"], "seriya bo'sh bo'lmasligi kerak"

        bad = await api_analytics(_Req(OWNER, {"range": "bogus"}))
        assert bad.status == 400
        bad_dates = await api_analytics(
            _Req(OWNER, {"range": "custom", "start": "2026-05-01", "end": "2026-01-01"})
        )
        assert bad_dates.status == 400
        assert (await api_analytics(_Req(OUTSIDER))).status == 403
    finally:
        await db.close()


def test_analytics_api_permissions() -> None:
    asyncio.run(_endpoints())
