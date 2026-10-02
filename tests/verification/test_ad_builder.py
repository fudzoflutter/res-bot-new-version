"""Ad Builder verification — reklama yaratish, preview, test yuborish,
progressning saqlanishi va "Retry Failed".

Tekshiriladi:

* media (photo/video/animation) + caption + 1..10 tugma (matn, URL, stil);
* 10 tadan ko'p tugma RAD ETILADI; eski ``Matn - URL`` formati ham ishlaydi;
* preview yuboriladigan xabar tuzilishini ko'rsatadi;
* TEST yuborish FAQAT bitta oluvchiga ketadi, broadcast BOSHLANMAYDI va
  statistikaga TA'SIR QILMAYDI;
* progress har bir oluvchi uchun bazada saqlanadi (PENDING/SENDING/SENT/FAILED);
* xato olgan oluvchilar ANIQLANADI; «Retry Failed» faqat ularga yuboradi va
  muvaffaqiyatli yuborilganlar TAKRORLANMAYDI;
* restart/deploy: yarim qolgan SENDING qatorlar FAILED bo'ladi va tiklash
  HECH NARSA yubormaydi (dublikat yo'q).
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

# Testlar tez bo'lsin: yuborishlar orasidagi pauza yo'q (rate limit baribir
# telegram_api.call orqali qayta urinishlar bilan boshqariladi).
os.environ.setdefault("BROADCAST_SEND_DELAY", "0")

from _verification_helpers import OWNER, FakeBot, make_init_data  # noqa: E402

from app import web as web_mod  # noqa: E402
from app.database import db  # noqa: E402
from app.services import broadcast as engine  # noqa: E402
from app.services.broadcast import AdError, build_ad, build_markup, preview  # noqa: E402

_USERS = (7201, 7202, 7203)
_TARGET = 7300


class _Request:
    """Minimal aiohttp Request (signed initData + JSON body)."""

    def __init__(self, body: dict | None = None, bot=None) -> None:  # noqa: ANN001
        self.headers = {"X-Telegram-Init-Data": make_init_data(OWNER)}
        self.query = SimpleNamespace(get=lambda *a, **k: None)
        self.app = {"bot": bot}
        self._body = body

    async def json(self) -> dict | None:
        return self._body


def _reset_state() -> None:
    engine.state.clear()
    engine.state.update({
        "running": False, "total": 0, "sent": 0, "failed": 0, "pending": 0,
        "started_at": None, "finished_at": None, "last_error": "",
        "interrupted": False, "broadcast_id": "", "mode": "none", "requested_by": 0,
    })


async def _wait_finished(timeout: float = 20.0) -> dict:
    for _ in range(int(timeout / 0.02)):
        if not web_mod.broadcast_snapshot()["running"]:
            return web_mod.broadcast_snapshot()
        await asyncio.sleep(0.02)
    raise AssertionError("broadcast tugamadi (timeout)")


# ---------------------------------------------------------------------------
# 1) Ad modeli: media, caption, tugmalar (validatsiya)
# ---------------------------------------------------------------------------
def test_ad_builder_validation_and_limits() -> None:
    photo = build_ad({"caption": "salom", "media_url": "https://cdn/x.jpg"})
    assert photo.media_type == "photo" and photo.has_media

    video = build_ad({"caption": "salom", "media_url": "https://cdn/x.mp4"})
    assert video.media_type == "video"

    animation = build_ad({"media_url": "https://cdn/x.gif"})
    assert animation.media_type == "animation"

    declared = build_ad({"media_url": "https://cdn/noext", "media_type": "video"})
    assert declared.media_type == "video"

    # Caption-only (eski xatti-harakat saqlanadi).
    text_only = build_ad({"text": "faqat matn"})
    assert text_only.caption == "faqat matn" and not text_only.has_media

    # 1 tugma (matn + URL + stil).
    one = build_ad({
        "text": "x",
        "buttons": [{"text": "Kanal", "url": "https://t.me/kanal", "style": "primary"}],
    })
    assert len(one.buttons) == 1
    assert one.buttons[0].style == "primary"
    markup = build_markup(one)
    assert markup is not None
    button = markup.inline_keyboard[0][0]
    assert button.text == "Kanal" and button.url == "https://t.me/kanal"

    # 10 tugma — chegara.
    ten = build_ad({
        "text": "x",
        "buttons": [{"text": f"b{i}", "url": f"https://t.me/b{i}"} for i in range(10)],
    })
    assert len(ten.buttons) == 10

    # 11 tugma — RAD ETILADI.
    with pytest.raises(AdError):
        build_ad({
            "text": "x",
            "buttons": [{"text": f"b{i}", "url": f"https://t.me/b{i}"} for i in range(11)],
        })

    # Eski "Matn - https://link" formati ham qo'llab-quvvatlanadi.
    legacy = build_ad({"text": "hi", "buttons": "Kanal - https://t.me/k\nSayt - https://x.com"})
    assert len(legacy.buttons) == 2
    assert legacy.buttons[1].url == "https://x.com"

    # Xato holatlar.
    for payload in (
        {},                                                  # bo'sh
        {"text": "x", "media_url": "ftp://x/y.jpg"},         # noto'g'ri protokol
        {"text": "x", "buttons": [{"text": "a", "url": "nope"}]},   # URL xato
        {"text": "x", "buttons": [{"text": "a", "url": "https://x", "style": "rainbow"}]},
        {"text": "x", "buttons": [{"url": "https://x"}]},    # matn yo'q
        {"caption": "c" * 2000},                             # juda uzun caption
    ):
        with pytest.raises(AdError):
            build_ad(payload)

    assert build_markup(text_only) is None


def test_ad_preview_shows_real_structure() -> None:
    ad = build_ad({
        "caption": "🔥 Reklama matni",
        "media_url": "https://cdn/p.jpg",
        "buttons": [
            {"text": "Kanal", "url": "https://t.me/kanal", "style": "success"},
            {"text": "Sayt", "url": "https://example.com"},
        ],
    })
    data = preview(ad)
    text = data["preview_text"]
    assert "MEDIA [photo]: https://cdn/p.jpg" in text
    assert "🔥 Reklama matni" in text
    assert "2/10" in text
    assert "Kanal -> https://t.me/kanal [success]" in text
    assert data["buttons_count"] == 2
    assert data["media_type"] == "photo"


# ---------------------------------------------------------------------------
# 2) Preview + test yuborish (endpoint darajasi)
# ---------------------------------------------------------------------------
async def _preview_and_test_send() -> None:
    await db.init()
    _reset_state()
    try:
        bot = FakeBot()
        await db.upsert_user(_TARGET, "target", "Target", None)
        advertisement = {
            "caption": "Test reklama",
            "media_url": "https://cdn/p.jpg",
            "buttons": [{"text": "Kanal", "url": "https://t.me/kanal", "style": "primary"}],
        }

        preview_resp = await web_mod.api_broadcast_preview(_Request(advertisement, bot))
        assert preview_resp.status == 200
        payload = json.loads(preview_resp.body)
        assert payload["buttons_count"] == 1
        assert payload["recipients"] >= 1
        assert "Test reklama" in payload["preview_text"]

        # 11 tugma -> 400 (server-side rad etish).
        too_many = dict(advertisement)
        too_many["buttons"] = [
            {"text": f"b{i}", "url": f"https://t.me/b{i}"} for i in range(11)
        ]
        assert (await web_mod.api_broadcast_preview(_Request(too_many, bot))).status == 400

        # --- TEST YUBORISH ------------------------------------------------
        before_state = dict(web_mod.broadcast_snapshot())
        resp = await web_mod.api_broadcast_test(_Request(advertisement, bot))
        assert resp.status == 200
        assert json.loads(resp.body)["target"] == OWNER
        assert bot.sent == [("photo", OWNER)], bot.sent
        assert not web_mod.broadcast_snapshot()["running"]
        after_state = dict(web_mod.broadcast_snapshot())
        for key in ("total", "sent", "failed", "broadcast_id", "running"):
            assert after_state[key] == before_state[key], key
        assert after_state["broadcast_id"] == "", "test yuborish broadcast yaratmaydi"

        # Boshqa oluvchiga test (test_recipient).
        bot.sent.clear()
        with_target = dict(advertisement)
        with_target["test_recipient"] = _TARGET
        assert (await web_mod.api_broadcast_test(_Request(with_target, bot))).status == 200
        assert bot.sent == [("photo", _TARGET)], bot.sent

        # Statistikaga ta'sir yo'q: yangi broadcast qatori yaratilmagan.
        counts = await db.broadcast_counts(after_state["broadcast_id"] or "none")
        assert counts["total"] == 0

        logs = await db.recent_activity_logs(limit=20)
        assert any(row["event_type"] == "broadcast_test_sent" for row in logs)

        # Yetkazilmagan test -> 502 (broadcast boshlanmaydi).
        bot.forbidden.add(OWNER)
        assert (await web_mod.api_broadcast_test(_Request(advertisement, bot))).status == 502
        assert not web_mod.broadcast_snapshot()["running"]
    finally:
        _reset_state()
        await db.close()


def test_preview_and_test_send_do_not_start_broadcast() -> None:
    asyncio.run(_preview_and_test_send())


# ---------------------------------------------------------------------------
# 3) Progress: bazada saqlanadi, xato olganlar aniq
# ---------------------------------------------------------------------------
async def _progress_and_retry() -> None:
    await db.init()
    _reset_state()
    try:
        for uid in _USERS:
            await db.upsert_user(uid, f"u{uid}", "Ad", None)

        bot = FakeBot()
        bot.forbidden.add(_USERS[1])  # bitta oluvchi doimiy xato oladi

        advertisement = {
            "caption": "Reklama",
            "buttons": [{"text": "Kanal", "url": "https://t.me/kanal"}],
        }
        resp = await web_mod.api_broadcast(_Request(advertisement, bot))
        assert resp.status == 202, resp.status
        accepted = json.loads(resp.body)
        broadcast_id = accepted["broadcast_id"]
        assert accepted["total"] == accepted["status"]["total"] >= len(_USERS)

        # Boshlanish holati DARHOL bazada (deploy o'lsa ham yo'qolmaydi).
        stored = json.loads(await db.get_setting(engine.BROADCAST_STATE_KEY, "{}"))
        assert stored["running"] is True and stored["total"] == accepted["total"]

        # Takroriy bosish -> 409.
        assert (await web_mod.api_broadcast(_Request(advertisement, bot))).status == 409

        final = await _wait_finished()
        assert final["running"] is False
        assert final["sent"] + final["failed"] == final["total"]
        assert final["failed"] == 1, final

        # Xato olgan oluvchi ANIQLANADI; muvaffaqiyatlilar ham.
        failed_ids = await db.broadcast_ids(broadcast_id, "FAILED")
        sent_ids = await db.broadcast_ids(broadcast_id, "SENT")
        assert failed_ids == [_USERS[1]]
        assert all(uid in sent_ids for uid in (_USERS[0], _USERS[2]))

        counts = await db.broadcast_counts(broadcast_id)
        assert counts["failed"] == 1
        assert counts["pending"] == 0
        assert counts["sent"] == counts["total"] - 1

        status_payload = json.loads(
            (await web_mod.api_broadcast_status(_Request(None, bot))).body
        )
        assert status_payload["failed"] == 1
        assert _USERS[1] in status_payload["failed_sample"]

        # --- RETRY FAILED: faqat xato olganlarga ---------------------------
        bot.forbidden.clear()
        sent_before = {uid: bot.sent.count(("message", uid)) for uid in _USERS}
        retry = await web_mod.api_broadcast_retry(_Request({}, bot))
        assert retry.status == 202, retry.status
        assert json.loads(retry.body)["total"] == 1

        final = await _wait_finished()
        assert final["running"] is False and final["total"] == 1
        assert bot.sent.count(("message", _USERS[1])) == 1
        # Muvaffaqiyatli yuborilganlar TAKRORLANMADI.
        assert bot.sent.count(("message", _USERS[0])) == sent_before[_USERS[0]]
        assert bot.sent.count(("message", _USERS[2])) == sent_before[_USERS[2]]

        counts = await db.broadcast_counts(broadcast_id)
        assert counts["failed"] == 0
        assert counts["pending"] == 0
        assert counts["sent"] == counts["total"]

        # Xato qolmadi -> yana retry 400.
        assert (await web_mod.api_broadcast_retry(_Request({}, bot))).status == 400
    finally:
        _reset_state()
        await db.close()


def test_progress_persistence_and_retry_failed() -> None:
    asyncio.run(_progress_and_retry())


# ---------------------------------------------------------------------------
# 4) Restart/deploy: SENDING -> FAILED, dublikat YO'Q
# ---------------------------------------------------------------------------
async def _recovery() -> None:
    await db.init()
    _reset_state()
    try:
        broadcast_id = "bc_adbuilder_recovery"
        await db.broadcast_seed(broadcast_id, [_USERS[0], _USERS[1]])
        await db.broadcast_mark(broadcast_id, _USERS[0], "SENT")
        # Jarayon SENDING paytida o'ldi (holat noma'lum).
        await db.broadcast_mark(broadcast_id, _USERS[1], "SENDING")
        await engine.save_ad(broadcast_id, build_ad({"text": "reklama"}))

        await db.set_setting(
            engine.BROADCAST_STATE_KEY,
            json.dumps({
                "running": True, "total": 2, "sent": 1, "failed": 0,
                "started_at": 1.0, "finished_at": None, "last_error": "",
                "broadcast_id": broadcast_id, "mode": "broadcast",
                "requested_by": OWNER, "v": engine.BROADCAST_STATE_VERSION,
            }),
        )

        bot = FakeBot()
        assert await web_mod.recover_broadcast_state() is True
        snapshot = web_mod.broadcast_snapshot()
        assert snapshot["running"] is False
        assert snapshot["interrupted"] is True
        assert snapshot["broadcast_id"] == broadcast_id
        assert not bot.sent, "tiklash HECH NARSA yubormaydi (dublikat yo'q)"

        counts = await db.broadcast_counts(broadcast_id)
        assert counts["sent"] == 1
        assert counts["failed"] == 1, "yarim qolgan SENDING FAILED bo'lishi kerak"
        assert counts["pending"] == 0

        # Retry Failed faqat holati noma'lum bo'lganini qayta yuboradi.
        assert (await web_mod.api_broadcast_retry(_Request({}, bot))).status == 202
        final = await _wait_finished()
        assert final["total"] == 1
        assert bot.sent == [("message", _USERS[1])]
        assert bot.sent.count(("message", _USERS[0])) == 0
        counts = await db.broadcast_counts(broadcast_id)
        assert counts["sent"] == 2 and counts["failed"] == 0
    finally:
        _reset_state()
        await db.close()


def test_restart_recovery_marks_unknown_and_never_duplicates() -> None:
    asyncio.run(_recovery())
