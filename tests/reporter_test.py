"""
Offline unit test for the activity reporter (no Telegram network, no DB).

Run from the project root:

    python tests/reporter_test.py

It plugs a FakeBot + FakeDB into Reporter and checks the STRICT rules:

1. business_message (sent)          -> NO report, only cached (text & media)
2. owner edits own text             -> NO report (cache refreshed)
3. owner edits own media caption    -> NO report (cache refreshed)
4. partner edits text               -> ✏️ report (📱 Default old / 📲 Edited new)
5. partner edits media caption      -> NO report (cache refreshed only)
6. owner deletes own message        -> NO report
7. partner deletes text             -> 🗑 report with the FULL original text
8. partner deletes media            -> cached file RE-SENT
                                       (photo/video/GIF/sticker/voice/circular video)
9. delete of unknown (pre-connect)  -> silently skipped
10. Who shows @username when available, name/mention otherwise
11. connection lookup is cached (1 DB read) and invalidated on a
    business_connection change
12. HAQIQIY aiogram yangiliklari (business_message JSON) — ovozli xabar va
    dumaloq video jim keshlanadi va o'chirilganda AYNAN o'sha file_id bilan
    qayta yuboriladi (foydalanuvchi shikoyati: "ovozli xabar va dumaloq
    video saqlanmayapti / qayta yuborilmayapti").
13. O'CHIRILISH VAQTI — hisobotlarda Toshkent (UTC+5) vaqtida ko'rsatiladi
    (server UTC bo'lsa ham 5 soatga surilmaydi), bir update uchun BIR MARTA
    olinadi va sekin DB/fayl yuklash uni o'zgartirib yubormaydi.
14. TEZ O'CHIRISH (asosiy xato): suhbatdosh xabarni yuborib DARHOL o'chirsa,
    o'chirish yangilamasi DB kesh yozuvidan OLDIN ishlanadi (aiogram
    update'larni parallel bajaradi).  Xotiradagi tezkor kesh tufayli hisobot
    baribir chiqadi — matn ham, ovozli xabar ham, dumaloq video ham.
15. ASL KO'RINISH RAD ETILSA: ovozli xabar / dumaloq video uchun Telegram
    xato qaytarsa (HAQIQIY Telegramda tekshirilgan: VOICE_MESSAGES_FORBIDDEN),
    fayl baytlari yuklab olinib qayta yuboriladi — video/dumaloq video VIDEO
    bo'lib, ovoz esa neytral nomli fayl (voice.bin) bo'lib ketadi (Telegram
    turni FAYL NOMI bo'yicha aniqlaydi), va SABAB (+ qaysi sozlamani ochish
    kerakligi) izohda ko'rinadi — hech narsa jim yo'qolmaydi.
"""

from __future__ import annotations

import sys as _sys

# Windows konsolida emoji chop etish uchun (cp1252 UnicodeEncodeError bermasin).
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import io
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram.types import Update  # noqa: E402

from app.services import reporter as rep  # noqa: E402
from app.services.reporter import (  # noqa: E402
    Reporter,
    clear_instant_cache,
    invalidate_connection,
)

OWNER_ID = 1000
PARTNER_ID = 2000
ADMIN_ID = 3000
OWNER_CHAT = 1000
CHAT_ID = 555


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class FakeMessage:
    """Minimal stand-in for aiogram Message."""

    def __init__(
        self,
        mid: int,
        from_id: int,
        text: Optional[str] = None,
        media: Optional[dict] = None,
        caption: Optional[str] = None,
        first_name: str = "Partner",
        username: Optional[str] = "juratbek",
    ):
        self.message_id = mid
        self.business_connection_id = "conn-1"
        self.chat = SimpleChat(CHAT_ID, first_name=first_name, username=username)
        self.from_user = SimpleUser(from_id, first_name, username)
        self.text = text
        self.caption = caption
        self._media = media or {}

    def __getattr__(self, name: str) -> Any:
        # aiogram messages simply lack absent content attributes.
        if name in {
            "sticker", "animation", "photo", "video", "voice", "video_note",
            "audio", "document",
        }:
            return self._media.get(name)
        raise AttributeError(name)


class SimpleChat:
    def __init__(self, cid: int, first_name: str = "", username: Optional[str] = None):
        self.id = cid
        self.type = "private"
        self.title = None
        self.first_name = first_name
        self.username = username


class SimpleUser:
    def __init__(self, uid: int, first_name: str = "", username: Optional[str] = None):
        self.id = uid
        self.first_name = first_name
        self.username = username


class SimpleDeleted:
    def __init__(self, mids: list[int]):
        self.business_connection_id = "conn-1"
        self.chat = SimpleChat(CHAT_ID, first_name="Partner", username="juratbek")
        self.message_ids = mids


class FakeBot:
    """Records every outbound call instead of touching Telegram."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []  # (method, caption/text)
        # SUHBAT (manzil): har bir yuborilgan xabar kimga ketgani.  Bu ro'yxat
        # aynan maxfiylikni isbotlaydi — hisobot EGAGA ketishi, ADMINGA emas.
        self.recipients: list[int] = []
        # Qayta yuborilgan fayllar: (method, file_id) — fayl AYNAN o'sha
        # keshlangan file_id bilan ketganini isbotlash uchun.
        self.files: list[tuple[str, str]] = []
        # Yuklab olingan baytlar bilan ketgan fayllar: (method, data, nomi).
        self.uploads: list[tuple[str, bytes, str]] = []
        self.downloaded: list[str] = []
        self.fail_media = False  # True -> photo yuborish xato beradi (fallback)
        # Muayyan metodlarni "rad etish" (Telegram xatosi kabi):
        # masalan {"send_voice"}, {"send_voice", "send_audio"}.
        self.fail_methods: set[str] = set()
        self.fail_text = "Telegram server says - Bad Request: VOICE_MESSAGES_FORBIDDEN"
        self.fail_download = False

    def _guard(self, method: str) -> None:
        if method in self.fail_methods:
            raise RuntimeError(self.fail_text)

    async def download(self, file: Any, **kw: Any) -> Any:
        """file_id -> baytlar (zaxira yo'l shu yoqdan foydalanadi)."""
        if self.fail_download:
            raise RuntimeError("fayl yuklab olinmadi (test)")
        self.downloaded.append(str(file))
        return io.BytesIO(b"BYTES:" + str(file).encode())

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
        self.recipients.append(chat_id)
        self.sent.append(("message", text))

    async def send_photo(self, chat_id: int, photo: str, caption: str = "", **kw: Any) -> None:
        self.recipients.append(chat_id)
        if self.fail_media:
            raise RuntimeError("media yuborilmadi (test)")
        self.sent.append(("photo", caption))
        self.files.append(("photo", photo))

    async def send_video(self, chat_id: int, video: Any, caption: str = "", **kw: Any) -> None:
        self.recipients.append(chat_id)
        self._guard("send_video")
        self.sent.append(("video", caption))
        if isinstance(video, str):
            self.files.append(("video", video))
        else:  # yuklangan baytlar
            self.uploads.append(("video", video.data, video.filename))

    async def send_animation(self, chat_id: int, animation: str, caption: str = "", **kw: Any) -> None:
        self.recipients.append(chat_id)
        self.sent.append(("animation", caption))
        self.files.append(("animation", animation))

    async def send_sticker(self, chat_id: int, sticker: str, **kw: Any) -> None:
        self.recipients.append(chat_id)
        self.sent.append(("sticker", ""))
        self.files.append(("sticker", sticker))

    async def send_voice(self, chat_id: int, voice: str, caption: str = "", **kw: Any) -> None:
        self.recipients.append(chat_id)
        self._guard("send_voice")
        self.sent.append(("voice", caption))
        self.files.append(("voice", voice))

    async def send_video_note(self, chat_id: int, video_note: str, **kw: Any) -> None:
        self.recipients.append(chat_id)
        self._guard("send_video_note")
        # Dumaloq videoga caption yozib bo'lmaydi — izoh alohida xabar bo'ladi.
        self.sent.append(("video_note", ""))
        self.files.append(("video_note", video_note))

    async def send_audio(self, chat_id: int, audio: Any, caption: str = "", **kw: Any) -> None:
        self.recipients.append(chat_id)
        self._guard("send_audio")
        self.sent.append(("audio", caption))
        self.uploads.append(("audio", audio.data, audio.filename))

    async def send_document(self, chat_id: int, document: Any, caption: str = "", **kw: Any) -> None:
        self.recipients.append(chat_id)
        if isinstance(document, str):  # file_id bilan qayta yuborish (musiqa/fayl)
            self._guard("send_document_file_id")
            self.sent.append(("document", caption))
            self.files.append(("document", document))
        else:  # yuklangan baytlar (zaxira yo'l)
            self._guard("send_document")
            self.sent.append(("document", caption))
            self.uploads.append(("document", document.data, document.filename))


class FakeDB:
    """In-memory mirror of only the DB calls Reporter makes."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._next_id = 1
        # Ulanish holati (test ichida o'zgartiriladi) + nechta marta
        # o'qilgani — kesh ishlayotganini isbotlash uchun.
        self.connection_calls = 0
        self.connection_enabled = True
        self.connection_user_id = OWNER_ID
        self.connection_chat_id = OWNER_CHAT

    # -- parallel helper + settings (limit disabled by default) -------------
    async def gather(self, *aws: Any) -> list[Any]:
        return list(await asyncio.gather(*aws))

    async def get_setting_cached(self, key: str, default: str = "") -> str:
        return default  # limit:{id} default "0" = unlimited

    async def count_user_events_since(
        self, user_id: int, since: Any, event_types: Optional[list[str]] = None
    ) -> int:
        return 0  # limit check always passes in this test

    # -- connections/users (owner approved, partner known) -----------------
    async def get_connection(self, cid: str) -> Optional[dict]:
        self.connection_calls += 1
        return {
            "user_id": self.connection_user_id,
            "user_chat_id": self.connection_chat_id,
            "is_enabled": self.connection_enabled,
        }

    async def get_user(self, user_id: int) -> Optional[dict]:
        if user_id == OWNER_ID:
            return {"user_id": OWNER_ID, "username": "owner",
                    "first_name": "Owner", "access_status": "approved"}
        if user_id == PARTNER_ID:
            return {"user_id": PARTNER_ID, "username": "juratbek",
                    "first_name": "Juratbek", "access_status": "approved"}
        return None

    # -- events -------------------------------------------------------------
    async def add_event(
        self, user_id: int, event_type: str, details: str,
        chat_id: Optional[int] = None, chat_title: Optional[str] = None,
        message_id: Optional[int] = None, sender_id: Optional[int] = None,
        business_connection_id: Optional[str] = None,
    ) -> None:
        self.events.append({
            "id": self._next_id, "user_id": user_id, "event_type": event_type,
            "details": details, "chat_id": chat_id, "chat_title": chat_title,
            "message_id": message_id, "sender_id": sender_id,
            "business_connection_id": business_connection_id,
        })
        self._next_id += 1

    async def get_event_by_message(
        self, chat_id: int, message_id: int,
        business_connection_id: Optional[str] = None,
    ) -> Optional[dict]:
        for row in reversed(self.events):
            if (
                row["chat_id"] == chat_id
                and row["message_id"] == message_id
                and (
                    business_connection_id is None
                    or row.get("business_connection_id") == business_connection_id
                )
            ):
                return row
        return None

    async def update_event_details(
        self, chat_id: int, message_id: int, details: str,
        business_connection_id: Optional[str] = None,
    ) -> bool:
        row = await self.get_event_by_message(
            chat_id, message_id, business_connection_id
        )
        if row:
            row["details"] = details
            return True
        return False


# --------------------------------------------------------------------------
# Test scenarios
# --------------------------------------------------------------------------
async def run_all() -> None:
    # Scenario A: partner sends text, then edits it, then deletes it.
    r = Reporter(FakeBot())
    monkeypatch_db = FakeDB()
    rep.db = monkeypatch_db  # type: ignore[assignment]

    incoming = FakeMessage(11, PARTNER_ID, text="Okay, darling")
    await r.report_incoming(incoming)
    assert r.bot.sent == [], "sent messages must NOT be reported"
    cached = await monkeypatch_db.get_event_by_message(CHAT_ID, 11)
    assert cached and cached["details"] == "Okay, darling", "text must be cached"

    # Partner edits the text.
    edited = FakeMessage(11, PARTNER_ID, text="Okay, darling!")
    await r.report_edited(edited)
    msgs = [t for kind, t in r.bot.sent if kind == "message"]
    assert len(msgs) == 1, f"exactly one edit report expected, got {len(msgs)}"
    report = msgs[0]
    assert "tahrirlandi" in report and "Okay, darling" in report
    # Shablon endi <blockquote> ishlatadi (app/utils/texts.REPORT_EDIT).
    assert "<b>Default:</b>" in report and "<blockquote>Okay, darling</blockquote>" in report
    assert "<b>Edited:</b>" in report and "<blockquote>Okay, darling!</blockquote>" in report
    assert "@juratbek" in report, "Who must show the username"
    assert "Chat: <b>Partner</b>" in report and "Vaqt: <b>" in report
    row = await monkeypatch_db.get_event_by_message(CHAT_ID, 11)
    assert row["details"] == "Okay, darling!", "cache must hold the latest text"

    # Partner deletes the (edited) message -> report shows the latest text.
    dels = SimpleDeleted([11])
    await r.report_deleted(dels)
    msgs = [t for kind, t in r.bot.sent if kind == "message"]
    assert len(msgs) == 2, "delete report expected"
    assert "o'chirildi" in msgs[1] and "Okay, darling!" in msgs[1]

    # Scenario B: owner's OWN actions must be silent.
    r2 = Reporter(FakeBot())
    own = FakeMessage(21, OWNER_ID, text="my own words")
    await r2.report_incoming(own)
    await r2.report_edited(FakeMessage(21, OWNER_ID, text="my own words!"))
    await r2.report_deleted(SimpleDeleted([21]))
    assert r2.bot.sent == [], "owner's own edits/deletes must not be reported"
    row = await monkeypatch_db.get_event_by_message(CHAT_ID, 21)
    assert row and row["details"] == "my own words!", "owner cache must refresh"

    # Scenario B2: ADMIN (ega ham, suhbatdosh ham EMAS) o'z tahriri va
    # o'chirishi ham JIM — xuddi egadek bir xil filtr.  Suhbatdosh esa
    # baribir hisobot qilinadi (tahrir ham, o'chirish ham).
    r2b = Reporter(FakeBot())
    object.__setattr__(rep.settings, "admin_ids_extra", (ADMIN_ID,))
    admin_own = FakeMessage(22, ADMIN_ID, text="admin words")
    await r2b.report_incoming(admin_own)
    await r2b.report_edited(FakeMessage(22, ADMIN_ID, text="admin words!"))
    await r2b.report_deleted(SimpleDeleted([22]))
    assert r2b.bot.sent == [], "adminning o'z tahriri/o'chirishi hisobot BO'LMASLIGI kerak"

    await r2b.report_incoming(FakeMessage(23, PARTNER_ID, text="partner words"))
    await r2b.report_edited(FakeMessage(23, PARTNER_ID, text="partner words!"))
    msgs = [t for kind, t in r2b.bot.sent if kind == "message"]
    assert len(msgs) == 1 and "partner words" in msgs[0], \
        "suhbatdosh tahriri baribir hisobot qilinishi kerak"
    await r2b.report_deleted(SimpleDeleted([23]))
    msgs = [t for kind, t in r2b.bot.sent if kind == "message"]
    assert len(msgs) == 2 and "partner words" in msgs[1], \
        "suhbatdosh o'chirishi baribir hisobot qilinishi kerak"
    object.__setattr__(rep.settings, "admin_ids_extra", ())
    print("Scenario B2 (admin o'z tahriri/o'chirishi jim, suhbatdosh hisobot) OK ✅")

    # Scenario C: partner media -> only a DELETE triggers a report (re-send).
    r3 = Reporter(FakeBot())
    photo = FakeMessage(
        31, PARTNER_ID,
        media={"photo": [SimplePhoto("fid_31")]},
        caption="look at this",
    )
    await r3.report_incoming(photo)
    assert r3.bot.sent == [], "photo send must NOT be reported"
    await r3.report_edited(
        FakeMessage(31, PARTNER_ID, media={"photo": [SimplePhoto("fid_31")]},
                    caption="look at this (edit)")
    )
    assert r3.bot.sent == [], "media caption edit must NOT be reported"
    await r3.report_deleted(SimpleDeleted([31]))
    kinds = [kind for kind, _ in r3.bot.sent]
    assert kinds == ["photo"], f"cached photo must be re-sent, got {kinds}"
    assert "@juratbek" in r3.bot.sent[0][1]
    assert "o'chirildi" in r3.bot.sent[0][1]

    # Video / GIF / sticker / voice / circular video deletions re-send their kind.
    for index, (kind, method) in enumerate(
        (
            ("video", "video"),
            ("animation", "animation"),
            ("sticker", "sticker"),
            ("voice", "voice"),
            ("video_note", "video_note"),
        )
    ):
        r4 = Reporter(FakeBot())
        media_obj = SimplePhoto(f"fid_{kind}")
        # Har bir tur ALOHIDA message_id bilan (dedupe takroriy deb hisoblamasin).
        m = FakeMessage(40 + index, PARTNER_ID, media={kind: media_obj})
        await r4.report_incoming(m)
        assert r4.bot.sent == [], f"{kind} send must NOT be reported"
        await r4.report_deleted(SimpleDeleted([m.message_id]))
        expected = (
            # sticker/video_note cannot carry a caption -> it goes as its own message
            [method, "message"] if method in {"sticker", "video_note"}
            else [method]
        )
        assert [k for k, _ in r4.bot.sent] == expected, kind

    # Scenario F: voice keeps its caption; the kind label says what was deleted.
    r7 = Reporter(FakeBot())
    await r7.report_incoming(
        FakeMessage(71, PARTNER_ID, media={"voice": SimplePhoto("fid_v")},
                    caption="eslab qol")
    )
    assert r7.bot.sent == [], "voice send must NOT be reported"
    await r7.report_deleted(SimpleDeleted([71]))
    assert [k for k, _ in r7.bot.sent] == ["voice"], r7.bot.sent
    assert "Voice o'chirildi" in r7.bot.sent[0][1]
    assert "eslab qol" in r7.bot.sent[0][1], "voice caption must be preserved"

    r8 = Reporter(FakeBot())
    await r8.report_incoming(
        FakeMessage(72, PARTNER_ID, media={"video_note": SimplePhoto("fid_n")})
    )
    assert r8.bot.sent == [], "circular video send must NOT be reported"
    await r8.report_deleted(SimpleDeleted([72]))
    assert [k for k, _ in r8.bot.sent] == ["video_note", "message"], r8.bot.sent
    assert "Video note o'chirildi" in r8.bot.sent[1][1]

    # Scenario D: uncached (pre-connect) deletes produce NO report — lekin jim
    # ham qolmaydi: nega hisobot yo'qligini aytadigan BITTA diagnostika
    # xabari boradi (15 daqiqa oralig'ida takrorlanmaydi).
    #
    # Sabab: bu nusxa xabarni umuman ko'rmagan — demak uni boshqa nusxa
    # (eski build / server) qabul qilgan yoki xabar bot ishga tushishidan
    # oldin yuborilgan.  Aynan shu holat "ovoz/dumaloq video qaytmadi"
    # shikoyatining sababi bo'lgani uchun endi ko'rinadi.
    rep._last_uncached_warning = 0.0
    r5 = Reporter(FakeBot())
    await r5.report_deleted(SimpleDeleted([999]))
    assert [k for k, _ in r5.bot.sent] == ["message"], r5.bot.sent
    diag = r5.bot.sent[0][1]
    assert "topilmadi" in diag and "999" in diag, diag
    assert "🗑" not in diag, f"bu hisobot EMAS, diagnostika: {diag}"

    # Takroriy ogohlantirish bo'lmaydi (shovqin qilmaslik uchun).
    await r5.report_deleted(SimpleDeleted([998]))
    assert len(r5.bot.sent) == 1, r5.bot.sent

    # Interval o'tgach yana bir marta aytiladi.
    rep._last_uncached_warning -= rep.UNCACHED_WARNING_INTERVAL + 1
    await r5.report_deleted(SimpleDeleted([997]))
    assert len(r5.bot.sent) == 2, r5.bot.sent
    assert "997" in r5.bot.sent[1][1]

    # Scenario E: Who falls back to name/mention when username is missing.
    r6 = Reporter(FakeBot())
    await r6.report_incoming(FakeMessage(61, PARTNER_ID, text="hi",
                                         username=None, first_name="Juratbek"))
    await r6.report_edited(FakeMessage(61, PARTNER_ID, text="hi!",
                                       username=None, first_name="Juratbek"))
    report = [t for kind, t in r6.bot.sent if kind == "message"][0]
    assert "Juratbek" in report, report

    # Scenario G: connection lookup is cached (one DB read per connection) and
    # dropped when the connection changes.
    invalidate_connection()  # butun kesh tozalanadi (toza boshlanish)
    gdb = FakeDB()
    rep.db = gdb  # type: ignore[assignment]

    rg = Reporter(FakeBot())
    for mid in (81, 82, 83):
        await rg.report_incoming(FakeMessage(mid, PARTNER_ID, text=f"m{mid}"))
    assert gdb.connection_calls == 1, (
        f"connection must be read once, got {gdb.connection_calls}"
    )

    # Ulan/uz hodisasi keshni tozalaydi -> keyingi update qayta o'qiydi.
    invalidate_connection("conn-1")
    await Reporter(FakeBot()).report_incoming(
        FakeMessage(84, PARTNER_ID, text="m84")
    )
    assert gdb.connection_calls == 2, (
        "invalidate_connection must force a fresh read"
    )

    # Uzilgan ulanish keshlanmaydi: hisobot yo'q, keyingi update yana o'qiydi.
    gdb.connection_enabled = False
    invalidate_connection("conn-1")
    rb = Reporter(FakeBot())
    await rb.report_incoming(FakeMessage(91, PARTNER_ID, text="after off"))
    assert rb.bot.sent == [], "disabled connection must not report"
    assert gdb.connection_calls == 3

    gdb.connection_enabled = True
    await Reporter(FakeBot()).report_incoming(
        FakeMessage(92, PARTNER_ID, text="back on")
    )
    assert gdb.connection_calls == 4, (
        "a disabled connection must not be cached - next update re-reads it"
    )
    row = await gdb.get_event_by_message(CHAT_ID, 92)
    assert row and row["details"] == "back on", "re-enabled connection caches again"

    # Scenario H: HAQIQIY aiogram yangiliklari (JSON -> Update -> Message).
    # Bu qism aynan foydalanuvchi shikoyatini tekshiradi: "ovozli xabar va
    # dumaloq video saqlanmayapti va qayta yuborilmayapti".  Yo'q — ikkalasi
    # ham jim keshlanadi, o'chirilganda esa fayl AYNAN o'sha file_id bilan
    # qayta yuboriladi.
    def media_update(mid: int, kind: str, fid: str) -> dict:
        media = {"file_id": fid, "file_unique_id": f"u{mid}", "duration": 5,
                 "file_size": 1000}
        if kind == "video_note":
            media["length"] = 240
        return {
            "update_id": mid,
            "business_message": {
                "message_id": mid,
                "date": 1_760_000_000,
                "business_connection_id": "conn-1",
                "chat": {"id": CHAT_ID, "type": "private",
                         "first_name": "Partner", "username": "juratbek"},
                "from": {"id": PARTNER_ID, "is_bot": False,
                         "first_name": "Juratbek", "username": "juratbek"},
                kind: media,
            },
        }

    def delete_update(mid: int) -> dict:
        return {
            "update_id": 9000 + mid,
            "deleted_business_messages": {
                "business_connection_id": "conn-1",
                "chat": {"id": CHAT_ID, "type": "private",
                         "first_name": "Partner", "username": "juratbek"},
                "message_ids": [mid],
            },
        }

    hdb = FakeDB()
    rep.db = hdb  # type: ignore[assignment]
    invalidate_connection()
    rh = Reporter(FakeBot())

    for mid, kind, fid in ((501, "voice", "VOICE_FILE_ID"),
                           (502, "video_note", "NOTE_FILE_ID")):
        rh.bot.sent.clear()
        rh.bot.files.clear()

        incoming = Update.model_validate(media_update(mid, kind, fid)).business_message
        assert incoming is not None and getattr(incoming, kind) is not None, kind
        await rh.report_incoming(incoming)
        assert rh.bot.sent == [], f"{kind}: yuborish hisobot qilinmasligi kerak"

        row = await hdb.get_event_by_message(CHAT_ID, mid)
        assert row and row["event_type"] == kind, (kind, row)
        assert fid in row["details"], (kind, row)

        deleted = Update.model_validate(delete_update(mid)).deleted_business_messages
        assert deleted is not None
        await rh.report_deleted(deleted)
        assert [m for m, _ in rh.bot.files] == [kind], (kind, rh.bot.files)
        assert rh.bot.files[0][1] == fid, (
            f"{kind}: keshlangan file_id qayta yuborilishi kerak"
        )
        assert rh.bot.sent, f"{kind}: sarlavha (izoh) yuborilishi kerak"
        assert "o'chirildi" in " ".join(t for _, t in rh.bot.sent)

    # Voice izohi (caption) ham saqlanadi va qayta yuboriladi.
    rh.bot.sent.clear()
    rh.bot.files.clear()
    with_caption = media_update(503, "voice", "VOICE_FID_2")
    with_caption["business_message"]["caption"] = "eslab qol"
    await rh.report_incoming(
        Update.model_validate(with_caption).business_message
    )
    await rh.report_deleted(
        Update.model_validate(delete_update(503)).deleted_business_messages
    )
    assert rh.bot.files == [("voice", "VOICE_FID_2")], rh.bot.files
    assert "eslab qol" in rh.bot.sent[0][1], rh.bot.sent

    # Scenario I: O'CHIRILISH VAQTI — aniq va Toshkent (UTC+5) vaqtida.
    #
    # Foydalanuvchi shikoyati: "bot o'chirilgan media uchun noto'g'ri vaqt
    # ko'rsatyapti".  Sabab: vaqt `datetime.now()` (mashina/server soati)
    # bilan olinardi — Docker/Railway konteynerida bu UTC, ya'ni Toshkentdan
    # 5 soat ORQADA.  Endi vaqt Toshkent mintaqasida va update kelgan ZAHOTI
    # (DB so'rovlaridan oldin) bir marta olinadi.
    idb = FakeDB()
    rep.db = idb  # type: ignore[assignment]
    invalidate_connection()
    ri = Reporter(FakeBot())

    real_now = rep.now_report
    # "Soat" har chaqiriqda boshqa vaqt qaytaradi: hisobot BIRINCHI (eng
    # erta) qiymatni ishlatishi kerak, kechroq olinganini emas.
    stamps = [
        datetime(2026, 9, 16, 12, 34, 56, tzinfo=timezone.utc),  # -> 17:34:56
        datetime(2026, 9, 16, 15, 0, 0, tzinfo=timezone.utc),   # -> 20:00:00
        datetime(2026, 9, 16, 18, 0, 0, tzinfo=timezone.utc),   # -> 23:00:00
    ]
    clock = {"calls": 0}

    def fake_now() -> datetime:
        value = stamps[min(clock["calls"], len(stamps) - 1)]
        clock["calls"] += 1
        return value

    rep.now_report = fake_now  # type: ignore[assignment]
    try:
        # (a) MATN o'chirilishi
        clock["calls"] = 0
        await ri.report_incoming(FakeMessage(601, PARTNER_ID, text="o'chiriladi"))
        await ri.report_deleted(SimpleDeleted([601]))
        text_report = [t for k, t in ri.bot.sent if k == "message"][0]
        assert "O'chirilgan: <b>17:34:56</b>" in text_report, text_report
        assert clock["calls"] == 1, (
            f"vaqt bir marta olinishi kerak, {clock['calls']} marta olingan"
        )

        # (b) MEDIA o'chirilishi (izohdagi vaqt ham AYNI)
        ri.bot.sent.clear()
        clock["calls"] = 0
        await ri.report_incoming(
            FakeMessage(602, PARTNER_ID, media={"voice": SimplePhoto("V1")})
        )
        await ri.report_deleted(SimpleDeleted([602]))
        caption = ri.bot.sent[0][1]
        assert "O'chirilgan: <b>17:34:56</b>" in caption, caption
        assert "20:00:00" not in caption and "23:00:00" not in caption, caption
        assert clock["calls"] == 1, clock["calls"]

        # (c) Media QAYTA YUBORILMASA (file_id xato va baytlar ham yuklanmaydi)
        #     — matnli zaxira hisobot ham SHU vaqtni ko'rsatadi (fallback
        #     yo'lida ham vaqt yo'qolmaydi).
        fallback_bot = FakeBot()
        fallback_bot.fail_media = True   # photo file_id bilan ketmaydi
        fallback_bot.fail_download = True  # baytlar ham olinmaydi
        rf = Reporter(fallback_bot)
        clock["calls"] = 0
        await rf.report_incoming(
            FakeMessage(603, PARTNER_ID, media={"photo": [SimplePhoto("P1")]})
        )
        await rf.report_deleted(SimpleDeleted([603]))
        body = [t for k, t in rf.bot.sent if k == "message"][0]
        assert "O'chirilgan: <b>17:34:56</b>" in body, body

        # (d) Bir yangilamada bir nechta xabar — hammasi AYNI vaqtni ko'rsatadi.
        ri.bot.sent.clear()
        clock["calls"] = 0
        await ri.report_incoming(FakeMessage(604, PARTNER_ID, text="bir"))
        await ri.report_incoming(FakeMessage(605, PARTNER_ID, text="ikki"))
        await ri.report_deleted(SimpleDeleted([604, 605]))
        times = [t for k, t in ri.bot.sent if k == "message"]
        assert len(times) == 2, times
        assert all("O'chirilgan: <b>17:34:56</b>" in t for t in times), times
        assert clock["calls"] == 1, clock["calls"]

        # (e) TAHRIRLASH hisoboti ham shu mintaqada (UTC+5).
        ri.bot.sent.clear()
        clock["calls"] = 0
        await ri.report_incoming(FakeMessage(606, PARTNER_ID, text="eski"))
        await ri.report_edited(FakeMessage(606, PARTNER_ID, text="yangi"))
        edit = [t for k, t in ri.bot.sent if k == "message"][0]
        assert "Vaqt: <b>17:34:56</b>" in edit, edit
    finally:
        rep.now_report = real_now  # type: ignore[assignment]

    # (f) Mashina soat mintaqasidan MUSTAQIL: UTC + 5 soat (Toshkentda DST yo'q).
    from app.utils.timeutils import REPORT_UTC_OFFSET_HOURS, TZ_REPORT, hms

    assert REPORT_UTC_OFFSET_HOURS == 5, REPORT_UTC_OFFSET_HOURS
    assert str(TZ_REPORT) == "UTC+05:00", TZ_REPORT
    assert hms(datetime(2026, 9, 16, 12, 34, 56)) == "17:34:56", "naive = UTC"
    assert hms(datetime(2026, 9, 16, 7, 4, 56, tzinfo=timezone.utc)) == "12:04:56"
    # Devor soati (wall clock) farqi: UTC + 5.  Diqqat: ikkala vaqt bir xil
    # oniy paytni bildiradi (astimezone), shuning uchun tzinfo'siz qiymatlar
    # taqqoslanadi.
    uz_clock = rep.now_report().replace(tzinfo=None)
    utc_clock = datetime.now(timezone.utc).replace(tzinfo=None)
    delta = (uz_clock - utc_clock).total_seconds()
    assert abs(delta - 5 * 3600) < 5, delta

    print("Scenario I (o'chirilish vaqti — Toshkent UTC+5, bir marta) OK ✅")

    # Scenario J: TEZ O'CHIRISH — kesh yozuvi hali bazaga tushmagan bo'lsa ham
    # hisobot chiqadi.  Bu HAQIQIY xato edi: aiogram update'larni parallel
    # bajaradi, Supabase yozuvi esa ~1.2 s — suhbatdosh xabarni darhol
    # o'chirsa, o'chirish yangilamasi "keshda yo'q" deb JIM o'tib ketardi
    # (aynan foydalanuvchi shikoyati: matn keladi, ovoz/dumaloq video kelmaydi).
    class SlowFakeDB(FakeDB):
        """add_event sekin (Supabase ~1.2 s ni simulyatsiya qiladi)."""

        async def add_event(self, *args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(0.05)
            await super().add_event(*args, **kwargs)

    def cached_rows(slow_db: "SlowFakeDB", mid: int) -> list[dict]:
        return [e for e in slow_db.events if e["message_id"] == mid]

    slow = SlowFakeDB()
    rep.db = slow  # type: ignore[assignment]
    invalidate_connection()
    clear_instant_cache()
    rj = Reporter(FakeBot())

    async def start_incoming(mid: int, kw: dict) -> asyncio.Task:
        """Xabar handler'i ISHGA TUSHADI (xotiraga yozadi), DB yozuvi esa
        davom etmoqda.  Real hayotda ham shunday: xabar va o'chirish — alohida
        update'lar (alohida long-poll javoblari), shuning uchun handler har
        doim birinchi bo'lib ishga tushadi; xotiradagi yozuv esa handler'ning
        eng birinchi (await'siz) qadamidir.
        """
        kw = {"from_id": PARTNER_ID, **kw}
        task = asyncio.create_task(rj.report_incoming(FakeMessage(mid, **kw)))
        await asyncio.sleep(0)      # task boshlandi: xotira yozildi
        assert rep.recall(CHAT_ID, mid) is not None, "tezkor kesh darhol to'lishi kerak"
        return task

    # (a) OVOZLI XABAR: qayta yuborish tugashidan OLDIN o'chiriladi.
    sending = await start_incoming(801, {"media": {"voice": SimplePhoto("FAST_VOICE")}})
    assert cached_rows(slow, 801) == [], "DB yozuvi hali tugamagan bo'lishi kerak"
    await rj.report_deleted(SimpleDeleted([801]))
    assert rj.bot.files == [("voice", "FAST_VOICE")], (
        f"tez o'chirilgan ovoz qayta yuborilishi kerak, {rj.bot.files}"
    )
    # Hisobot aynan DB yozuvi YO'Q paytda chiqdi (yuqoridagi tekshiruv +
    # qayta yuborilgan fayl).  Orqa fondagi yozuv esa yo'qolmaydi.
    await sending
    assert cached_rows(slow, 801), "yozuv baribir bazaga tushadi (statistika)"

    # (b) DUMALOQ VIDEO — xuddi shunday.
    rj.bot.sent.clear()
    rj.bot.files.clear()
    sending = await start_incoming(802, {"media": {"video_note": SimplePhoto("FAST_NOTE")}})
    await rj.report_deleted(SimpleDeleted([802]))
    assert rj.bot.files == [("video_note", "FAST_NOTE")], rj.bot.files
    assert any("Video note o'chirildi" in t for k, t in rj.bot.sent if k == "message")
    await sending

    # (c) MATN — asl matn baribir ko'rsatiladi.
    rj.bot.sent.clear()
    sending = await start_incoming(803, {"text": "tez o'chdi"})
    await rj.report_deleted(SimpleDeleted([803]))
    # Matn HTML-escape qilinadi (o' -> &#x27;), shuning uchun bo'lak bo'yicha.
    assert any(
        "Asl matn" in t and "tez o" in t and "chdi" in t
        for k, t in rj.bot.sent
        if k == "message"
    ), rj.bot.sent
    await sending

    # (d) Egasining o'z xabari tez o'chirilsa ham JIM qoladi (talab).
    rj.bot.sent.clear()
    sending = await start_incoming(804, {"text": "o'zim yozdim", "from_id": OWNER_ID})
    await rj.report_deleted(SimpleDeleted([804]))
    assert rj.bot.sent == [], "ega o'z xabarini o'chirsa hisobot BO'LMASLIGI kerak"
    await sending

    # (e) Umuman keshda bo'lmagan xabar (aloqadan oldin yuborilgan) JIM o'tadi.
    rj.bot.sent.clear()
    await rj.report_deleted(SimpleDeleted([9999]))
    assert rj.bot.sent == [], "noma'lum xabar haqida hisobot bo'lmasligi kerak"

    clear_instant_cache()
    print("Scenario J (tez o'chirish — kesh poygasi) OK ✅")

    # Scenario K: OVOZLI XABAR / DUMALOQ VIDEO — Telegram ASL KO'RINISHNI rad
    # etsa ham hisobot yo'qolmaydi.
    #
    # Foydalanuvchi shikoyati: "ovozli xabar va dumaloq video qaytmayapti" —
    # keshlangan file_id esa HAQIQIY (get_file ishlaydi, baytlari yuklanadi).
    # Demak xato faylda emas, YUBORISHNING O'ZIDA: Telegram ovozli xabar va
    # dumaloq videoni qabul qiluvchining sozlamasiga qarab rad etadi
    # ("VOICE_MESSAGES_FORBIDDEN").  Eski kod bu xatoni INFO darajasida
    # yutib, matnli kartaga o'tardi — foydalanuvchi esa faqat "fayl kelmadi"
    # deb ko'rardi.  Endi: (1) asl ko'rinish; (2) rad etilsa baytlar yuklab
    # olinib FAYL (audio/video/document) sifatida yuboriladi; (3) sabab
    # izohda/kartada KO'RINADI.
    kdb = FakeDB()
    rep.db = kdb  # type: ignore[assignment]
    invalidate_connection()
    clear_instant_cache()

    # (a) OVOZ + "ovozli xabar" maxfiyligi.  Jonli tekshiruv (haqiqiy Telegram)
    #     shuni ko'rsatdi: bu holatda send_voice, send_audio va hatto
    #     .oga/.ogg/.opus FAYL ham rad etiladi (hammasi VOICE_MESSAGES_FORBIDDEN),
    #     faqat neytral nomli fayl o'tadi.  Shuning uchun audio urinmaymiz.
    rep._last_voice_hint = 0.0  # maslahat vaqti cheklovi testga xalaqit bermasin
    kb = FakeBot()
    kb.fail_methods = {"send_voice"}
    rk = Reporter(kb)
    await rk.report_incoming(
        FakeMessage(901, PARTNER_ID, media={"voice": SimplePhoto("VOICE_FID")})
    )
    await rk.report_deleted(SimpleDeleted([901]))
    assert kb.files == [], f"file_id bilan emas, fayl bilan ketishi kerak: {kb.files}"
    assert kb.downloaded == ["VOICE_FID"], kb.downloaded
    assert [u[0] for u in kb.uploads] == ["document"], kb.uploads
    method, data, name = kb.uploads[0]
    assert data.startswith(b"BYTES:"), data
    assert name == rep.VOICE_BLOCKED_FILE_NAME, name
    caption = [t for k, t in kb.sent if k == "document"][0]
    assert "VOICE_MESSAGES_FORBIDDEN" in caption, caption
    assert "fayl sifatida yuborildi" in caption, caption
    assert "Ovozli xabarlar" in caption, caption  # qaysi sozlamani ochish kerak
    assert "O'chirilgan: <b>" in caption, caption
    # Mazmun ketdi, shuning uchun "Xabar: <id>" kartasi YUBORILMAYDI.
    assert not any("Xabar:" in t for k, t in kb.sent if k == "message"), kb.sent

    # (b) Dumaloq video rad etildi -> VIDEO bo'lib ketadi.  Maxfiylik
    #     sozlamasi videoga TEGMAYDI — shuning uchun video uriniladi.
    kb2 = FakeBot()
    kb2.fail_methods = {"send_video_note"}
    rk2 = Reporter(kb2)
    await rk2.report_incoming(
        FakeMessage(902, PARTNER_ID, media={"video_note": SimplePhoto("NOTE_FID")})
    )
    await rk2.report_deleted(SimpleDeleted([902]))
    assert [u[0] for u in kb2.uploads] == ["video"], kb2.uploads
    assert kb2.uploads[0][2] == "video_note.mp4", kb2.uploads

    # (c) BOSHQA sabab (maxfiylik emas) -> avval AUDIO sifatida uriniladi.
    kb3 = FakeBot()
    kb3.fail_methods = {"send_voice"}
    kb3.fail_text = "Telegram server says - Bad Request: wrong file identifier"
    rk3 = Reporter(kb3)
    await rk3.report_incoming(
        FakeMessage(903, PARTNER_ID, media={"voice": SimplePhoto("V2")})
    )
    await rk3.report_deleted(SimpleDeleted([903]))
    assert [u[0] for u in kb3.uploads] == ["audio"], kb3.uploads
    assert kb3.uploads[0][2] == "voice.oga", kb3.uploads

    # (c2) Audio ham rad etilsa -> document (ASL nomi bilan, neytral emas -
    #      sabab maxfiylik emas).
    kb6 = FakeBot()
    kb6.fail_methods = {"send_voice", "send_audio"}
    kb6.fail_text = "Telegram server says - Bad Request: some other refusal"
    rk6 = Reporter(kb6)
    await rk6.report_incoming(
        FakeMessage(906, PARTNER_ID, media={"voice": SimplePhoto("V5")})
    )
    await rk6.report_deleted(SimpleDeleted([906]))
    assert [u[0] for u in kb6.uploads] == ["document"], kb6.uploads
    assert kb6.uploads[0][2] == "voice.oga", kb6.uploads

    # (d) Yuklab olish ham ishlamasa — sabab kartada (jim yo'qolmaydi).
    kb4 = FakeBot()
    kb4.fail_methods = {"send_voice"}
    kb4.fail_download = True
    rk4 = Reporter(kb4)
    await rk4.report_incoming(
        FakeMessage(904, PARTNER_ID, media={"voice": SimplePhoto("V3")})
    )
    await rk4.report_deleted(SimpleDeleted([904]))
    card = [t for k, t in kb4.sent if k == "message"]
    assert len(card) == 1 and "Xabar: <code>904</code>" in card[0], card
    assert "Faylni ham yuborib bo'lmadi" in card[0], card
    assert "VOICE_MESSAGES_FORBIDDEN" in card[0], card

    # (e) Ishlaydigan holatda QO'SHIMCHA yuklash bo'lmaydi — tezlik o'zgarmaydi.
    kb5 = FakeBot()
    rk5 = Reporter(kb5)
    await rk5.report_incoming(
        FakeMessage(905, PARTNER_ID, media={"voice": SimplePhoto("V4")})
    )
    await rk5.report_deleted(SimpleDeleted([905]))
    assert kb5.files == [("voice", "V4")], kb5.files
    assert kb5.uploads == [] and kb5.downloaded == [], (kb5.uploads, kb5.downloaded)

    clear_instant_cache()
    print("Scenario K (ovoz/dumaloq video rad etilsa — fayl zaxirasi) OK ✅")

    # Scenario L: KO'P FOYDALANUVCHI (300 ta ulanish) — HISOBOTLAR HECH QACHON
    # ADMINGA KETMAYDI, har biri AYNAN o'z ulanishining egasiga boradi.
    #
    # Ilgari bu qoida TEST QILINMAGAN edi: FakeBot manzilni (chat_id) saqlab
    # qo'ymasdi, shuning uchun hisobot adminga ketib qolsa ham barcha testlar
    # o'tib ketardi.  Endi har bir yuborilgan xabarning manzili tekshiriladi.
    ldb = FakeDB()
    rep.db = ldb  # type: ignore[assignment]
    invalidate_connection()
    clear_instant_cache()

    old_admin = rep.settings.admin_id
    # ``settings`` — frozen dataclass, shuning uchun object.__setattr__.
    object.__setattr__(rep.settings, "admin_id", 424242)  # ega ham, suhbatdosh ham EMAS
    try:
        lb = FakeBot()
        rl = Reporter(lb)
        sending = asyncio.create_task(
            rl.report_incoming(FakeMessage(1001, PARTNER_ID, text="maxfiy so'z"))
        )
        await rl.report_edited(FakeMessage(1001, PARTNER_ID, text="maxfiy so'z!"))
        await rl.report_deleted(SimpleDeleted([1001]))
        await sending

        assert lb.sent, "hisobot chiqishi kerak"
        assert set(lb.recipients) == {OWNER_CHAT}, (
            f"barcha hisobotlar FAQAT ega chatiga ({OWNER_CHAT}) ketishi kerak, "
            f"lekin manzillar: {lb.recipients}"
        )
        assert rep.settings.admin_id not in lb.recipients, (
            "hisobot admin chatiga ketdi — maxfiylik buzildi!"
        )

        # Ikkinchi (boshqa) ulanish — hisobot faqat O'SHA egaga boradi.
        OWNER2_CHAT = 3000
        ldb.connection_user_id = OWNER2_CHAT
        ldb.connection_chat_id = OWNER2_CHAT
        invalidate_connection("conn-1")
        lb2 = FakeBot()
        rl2 = Reporter(lb2)
        await rl2.report_incoming(FakeMessage(2001, PARTNER_ID, text="ikkinchi ega"))
        await rl2.report_deleted(SimpleDeleted([2001]))
        assert set(lb2.recipients) == {OWNER2_CHAT}, (
            f"hisobot faqat o'z egasiga ({OWNER2_CHAT}) ketishi kerak: "
            f"{lb2.recipients}"
        )
        assert OWNER_CHAT not in lb2.recipients, "hisobot boshqa egaga ketdi!"
    finally:
        object.__setattr__(rep.settings, "admin_id", old_admin)

    clear_instant_cache()
    print("Scenario L (ko'p foydalanuvchi — hisobot faqat egaga, adminga EMAS) OK ✅")

    # Scenario M: MUSIQA (audio) va FAYL (document) — talab: "musiqa bo'lsa
    # fayl, fayl bo'lsa fayl".  Ikkalasi ham jim keshlanadi va o'chirilganda
    # ASL FAYL ko'rinishida (file_id bilan) qayta yuboriladi.  Tanilmagan
    # boshqa yozma kontent (poll/location...) ham keshlanadi va jim qolmaydi.
    mdb = FakeDB()
    rep.db = mdb  # type: ignore[assignment]
    invalidate_connection()
    clear_instant_cache()
    mb = FakeBot()
    rm = Reporter(mb)

    # (a) MUSIQA: audio -> document (file_id bilan), izoh saqlanadi.
    await rm.report_incoming(
        FakeMessage(1101, PARTNER_ID,
                    media={"audio": SimpleMedia("AUDIO_FID", "song.mp3")},
                    caption="tingla")
    )
    assert mb.sent == [], "musiqa yuborilishi hisobot qilinmasligi kerak"
    row = await mdb.get_event_by_message(CHAT_ID, 1101)
    assert row and row["event_type"] == "audio" and "AUDIO_FID" in row["details"], row
    assert "song.mp3" in row["details"], row
    await rm.report_deleted(SimpleDeleted([1101]))
    assert [k for k, _ in mb.sent] == ["document"], mb.sent
    assert mb.files == [("document", "AUDIO_FID")], mb.files
    audio_caption = [t for k, t in mb.sent if k == "document"][0]
    assert "Audio o'chirildi" in audio_caption and "tingla" in audio_caption, audio_caption

    # (b) FAYL (document): ASL nom saqlanadi va qayta yuboriladi.
    mb.sent.clear()
    mb.files.clear()
    await rm.report_incoming(
        FakeMessage(1102, PARTNER_ID,
                    media={"document": SimpleMedia("DOC_FID", "hisobot.pdf")})
    )
    row = await mdb.get_event_by_message(CHAT_ID, 1102)
    assert row and row["event_type"] == "document", row
    assert "hisobot.pdf" in row["details"], row
    await rm.report_deleted(SimpleDeleted([1102]))
    assert [k for k, _ in mb.sent] == ["document"], mb.sent
    assert mb.files == [("document", "DOC_FID")], mb.files
    assert "File o'chirildi" in mb.sent[0][1], mb.sent

    # (c) ASL ko'rinish rad etilsa — baytlar ASL NOM bilan fayl bo'lib ketadi.
    mb2 = FakeBot()
    # Faqat file_id bilan yuborish rad etiladi; baytlarni qayta yuklash o'tadi.
    mb2.fail_methods = {"send_document_file_id"}
    mb2.fail_text = "Telegram server says - Bad Request: wrong file identifier"
    rm2 = Reporter(mb2)
    await rm2.report_incoming(
        FakeMessage(1103, PARTNER_ID,
                    media={"document": SimpleMedia("DOC_FILE_ID", "shartnoma.docx")})
    )
    await rm2.report_deleted(SimpleDeleted([1103]))
    assert [u[0] for u in mb2.uploads] == ["document"], mb2.uploads
    assert mb2.uploads[0][2] == "shartnoma.docx", mb2.uploads

    # (d) BOSHQA kontent (masalan poll) — kartochka chiqadi, jim qolmaydi.
    mb3 = FakeBot()
    rm3 = Reporter(mb3)
    other = FakeMessage(1104, PARTNER_ID)
    object.__setattr__(other, "content_type", "poll")
    await rm3.report_incoming(other)
    assert mb3.sent == [], "poll yuborilishi hisobot qilinmasligi kerak"
    await rm3.report_deleted(SimpleDeleted([1104]))
    assert [k for k, _ in mb3.sent] == ["message"], mb3.sent
    assert "o'chirildi" in mb3.sent[0][1], mb3.sent

    clear_instant_cache()
    print("Scenario M (musiqa/fayl/other — o'chirilganda qayta yuborish) OK ✅")

    print("REPORTER RULES TEST PASSED ✅  "
          "(silent sends, edit/delete-only reports, partner-only, usernames, "
          "connection cache, owner-only delivery, audio/document deletes)")


class SimplePhoto:
    def __init__(self, file_id: str) -> None:
        self.file_id = file_id


class SimpleMedia:
    """audio/document uchun: file_id + asl fayl nomi."""

    def __init__(self, file_id: str, file_name: Optional[str] = None) -> None:
        self.file_id = file_id
        self.file_name = file_name


if __name__ == "__main__":
    asyncio.run(run_all())
