"""
Hisobot xizmati (4-band — yangilangan talab).

Telegram business-update'larini ulanish egasining O'Z botiga yuboriladigan
xabarga aylantiradi.  Qoidalar QAT'IY:

* yuborilgan xabarlar FORVARD QILINMAYDI — tarkib jim KESHlanadi (DBda);
* matn xabari           -> faqat TAHRIRLANGANDA yoki O'CHIRILGANDA xabar;
* media (rasm/video/GIF/stiker/ovozli xabar/dumaloq video/musiqa/fayl) ->
  faqat O'CHIRILGANDA xabar (keshlangan fayl qayta yuboriladi);
* musiqa (audio) va fayl (document) ham ASL fayl ko'rinishida qayta yuboriladi;
* qolgan yozma kontent (so'rovnoma, joylashuv, kontakt, o'yin...) ham
  keshlanadi va o'chirilganda qisqa kartochka sifatida xabar qilinadi.
* hisobot faqat SUHBATDOSH hodisalari uchun: eganing o'z yuborgan/
  tahrirlagan/o'chirgan xabarlari hech qachon hisobot qilib berilmaydi.

Tahrirlash hisoboti namunadagi ko'rinishda:

    ✏️ Message edited

    👤 Who: @username
    📱 Default: eski matn
    📲 Edited: yangi matn
    💬 Chat: Alijon
    🕒 Time: 23:08:54

Har bir xabar DBda BITTA yozuv bilan saqlanadi: tahrirlashda yozuv JOYIDA
yangilanadi (update_event_details) — shu sababli keyingi o'chirish hisoboti
doim ENG OXIRGI tarkibni beradi.

Shaxsiylik: hisobotlar faqat ulanish EGASIGA boradi (admin emas!).
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from typing import Any, Optional

from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    BusinessMessagesDeleted,
    Chat,
    FSInputFile,
    Message,
    User as TgUser,
)

from app.config import settings
from app.database import db
from app.utils.telegram_api import call as tg_call
from app.utils.timeutils import now_iso


# Hisobotlardagi vaqtlar: Toshkent (UTC+5) vaqtida, server soatiga
# bog'liq bo'lmagan holda (app/utils/timeutils.py — bitta joyda sozlanadi).
from app.utils.formatting import esc, mention_by_id
from app.utils.texts import (
    NO_TEXT,
    E_CHAT,
    E_TRASH,
    REPORT_DELETED_MEDIA,
    REPORT_DELETED_MEDIA_CAPTION,
    REPORT_DELETED_TEXT,
    REPORT_EDIT,
    REPORT_FOOTER,
    REPORT_FOOTER_DELETED,
    REPORT_RESEND_FAILED,
    REPORT_RESEND_FILE_FORM,
    REPORT_UNCACHED,
    REPORT_VOICE_SETTING_HINT,
    TRUNCATED,
    UNKNOWN_CHAT,
    WHO_UNKNOWN,
)
from app.utils.timeutils import hms, now_report

logger = logging.getLogger(__name__)


def _is_panel_admin(user_id: Optional[int]) -> bool:
    """Env + panel orqali berilgan barcha admin rollarini bitta manbadan tekshiradi."""
    if not user_id:
        return False
    from app.services import admin_roles

    return admin_roles.is_admin(int(user_id))

# Hisobot xabarida ko'rsatiladigan matn chegarasi (DBda TO'LIQ saqlanadi).
MAX_TEXT = 350
MAX_TITLE = 64
MAX_BULK_DELETES = 50  # bir vaqtda o'chirilgan xabarlar ustidagi cheklov

# Katta fayllar: RAMni himoya qilish chegaralari.
#  * shu chegaradan kichik fayl — xotirada qayta ishlanadi;
#  * kattasi — vaqtincha FAYLGA oqim bilan yuklanadi (streaming) va
#    yuborilgach DARHOL o'chiriladi (cleanup).
MEDIA_MEMORY_LIMIT = 8 * 1024 * 1024          # 8 MB
MEDIA_MAX_DOWNLOAD = 100 * 1024 * 1024        # 100 MB — undan kattasi yuklanmaydi
MEDIA_DOWNLOAD_TIMEOUT = 180.0                 # sekund

# Takroriy (duplicate) hodisalarni yutish oynasi.
# Telegram ba'zan bir xil update'ni qayta yuboradi (poll qayta urinishi,
# ikki nusxa va h.k.) — bir xil tahrir/o'chirish IKKI MARTA hisobot
# qilinmasligi kerak.
DEDUPE_TTL_SECONDS = 600.0

# Hisobot statistikasi (admin panel "🤖 Bot Health" uchun).
REPORTER_STATS = {
    "received": 0,
    "edited": 0,
    "deleted_reports": 0,
    "uncached_deletes": 0,
    "duplicates_suppressed": 0,
    "cache_db_mismatch": 0,
    "media_file_fallback": 0,
    "media_too_large": 0,
    "bulk_truncated": 0,
}

# Keshda topilmagan o'chirishlar haqida ogohlantirish oralig'i (sekund).
# Bunday holat jimgina o'tkazib yuborilardi — foydalanuvchi "ovoz/dumaloq video
# kelmadi" deb ko'rardi, sababi esa ko'rinmasdi.  Endi 15 daqiqada ko'pi bilan
# bir marta xabar beriladi (shovqin qilmasligi uchun).
UNCACHED_WARNING_INTERVAL = 900
_last_uncached_warning = 0.0

# ---------------------------------------------------------------------------
# Hodisa turlari (DB qiymatlari)
# ---------------------------------------------------------------------------
EVENT_EDIT = "edit"
EVENT_DELETE = "delete"
EVENT_DELETE_MEDIA = "delete_media"
EVENT_STICKER = "sticker"
EVENT_PHOTO = "photo"
EVENT_VIDEO = "video"
EVENT_ANIMATION = "animation"  # GIF
EVENT_VOICE = "voice"          # ovozli xabar
EVENT_VIDEO_NOTE = "video_note"  # dumaloq (circular) video
EVENT_AUDIO = "audio"          # musiqa / audio fayl
EVENT_DOCUMENT = "document"    # har qanday yuklangan fayl
EVENT_OTHER = "other"          # qolgan yozma kontent (poll/location/game...)
EVENT_TEXT = "text"

KIND_LABELS = {
    EVENT_STICKER: "Sticker",
    EVENT_PHOTO: "Photo",
    EVENT_VIDEO: "Video",
    EVENT_ANIMATION: "GIF",
    EVENT_VOICE: "Voice",
    EVENT_VIDEO_NOTE: "Video note",
    EVENT_AUDIO: "Audio",
    EVENT_DOCUMENT: "File",
    EVENT_OTHER: "Message",
    EVENT_TEXT: "Message",
}

MEDIA_EVENTS = frozenset(
    {
        EVENT_STICKER,
        EVENT_PHOTO,
        EVENT_VIDEO,
        EVENT_ANIMATION,
        EVENT_VOICE,
        EVENT_VIDEO_NOTE,
        EVENT_AUDIO,
        EVENT_DOCUMENT,
    }
)

# Asl ko'rinishda qayta yuborib bo'lmaganda fayl NOMI (baytlar orqali
# yuboriladi).  Nom foydalanuvchiga ko'rinadi va Telegram turini shundan
# ham aniqlaydi.
FILE_NAMES = {
    EVENT_STICKER: "sticker.webm",
    EVENT_PHOTO: "photo.jpg",
    EVENT_VIDEO: "video.mp4",
    EVENT_ANIMATION: "animation.mp4",
    EVENT_VOICE: "voice.oga",
    EVENT_VIDEO_NOTE: "video_note.mp4",
    EVENT_AUDIO: "audio.mp3",
    EVENT_DOCUMENT: "document.bin",
}
DEFAULT_FILE_NAME = "file.bin"

# "OVOZLI XABAR" MAXFIYLIGI (qabul qiluvchining Telegram sozlamasi).
#
# Jonli tekshiruv (haqiqiy Telegram bilan) shuni ko'rsatdi: qabul qiluvchida
# "Ovozli xabarlar" maxfiylik sozlamasi tor bo'lsa, Telegram shu OVOZNI
# tanigan HAR QANDAY ko'rinishni rad etadi:
#     send_voice    -> VOICE_MESSAGES_FORBIDDEN
#     send_audio    -> VOICE_MESSAGES_FORBIDDEN
#     send_document(.oga/.ogg/.opus) -> VOICE_MESSAGES_FORBIDDEN
# ya'ni bitta ham shakl o'tmaydi.  Telegram turini FAYL NOMI bo'yicha
# aniqlaydi: neytral kengaytmali fayl (.bin) esa O'TADI — shuning uchun oxirgi
# chora shu (kontent o'zgarmaydi, faqat nomi audio deb tanilmaydi).
# Dumaloq video esa oddiy VIDEO sifatida bemalol ketadi (tekshirilgan).
VOICE_PRIVACY_MARKER = "VOICE_MESSAGES_FORBIDDEN"
VOICE_BLOCKED_FILE_NAME = "voice.bin"

# Bu maslahat (qaysi sozlamani ochish kerak) bir soatda ko'pi bilan bir marta
# qo'shiladi — har bir bloklangan ovoz uchun takrorlanib shovqin qilmasin.
VOICE_HINT_INTERVAL = 3600.0
_last_voice_hint = 0.0


def _voice_hint_due() -> bool:
    """Sozlama haqidagi maslahatni hozir qo'shish kerakmi?"""
    global _last_voice_hint
    now = time.monotonic()
    if _last_voice_hint and now - _last_voice_hint < VOICE_HINT_INTERVAL:
        return False
    _last_voice_hint = now
    return True


# ---------------------------------------------------------------------------
# Ulanish keshi (protsess ichida) — TEZLIK uchun.
#
# Har bir business-update'da `db.get_connection` chaqirish Supabase ustida
# ~200 ms turadi, holbuki ulanish FAQAT `business_connection` hodisasida
# o'zgaradi.  Shuning uchun faol ulanishning (owner_id, owner_chat) juftini
# eslab qolamiz va o'sha hodisada tozalaymiz:
#   app.handlers.business.on_connection -> invalidate_connection(...)
#
# TTL — ehtiyot chorasi: hodisa o'tkazib yuborilsa ham eski holat abadiy
# qolmaydi.  Uzilgan/yo'q ulanish KESHLANMAYDI: u keyingi update'da baribir
# qayta o'qiladi.
# ---------------------------------------------------------------------------
_CONNECTION_TTL_SECONDS = 300.0

# business_connection_id -> (monotonic vaqt, owner_id, owner_chat)
_connection_cache: dict[str, tuple[float, int, Optional[int]]] = {}

# Foydalanuvchi yozuvlari keshi (TTL) — "kim o'chirdi" nomini aniqlashda
# har bir xabar uchun DBga qayta murojaat qilmaslik uchun (N+1 oldini olish).
_USER_CACHE_TTL_SECONDS = 300.0
_USER_CACHE_MAX = 2_000
_user_cache: dict[int, tuple[float, Optional[dict]]] = {}


def invalidate_connection(connection_id: Optional[str] = None) -> None:
    """Ulanish keshini tozalash.

    * ``connection_id`` berilgan bo'lsa — faqat shu yozuv o'chiriladi
      (ulan / uz / ruxsat o'zgardi hodisasida chaqiriladi).
    * ``None`` bo'lsa — butun kesh tozalanadi (restart / testlar uchun).

    Shundan keyingi birinchi update DBdan YANGI holatni o'qiydi.
    """
    if connection_id is None:
        _connection_cache.clear()
    else:
        _connection_cache.pop(connection_id, None)


# ---------------------------------------------------------------------------
# TEZKOR KESH (xotira) — "darhol o'chirish" muammosining yechimi.
#
# aiogram update'larni PARALLEL bajaradi (handle_as_tasks=True): har bir
# update uchun alohida task ochiladi.  Supabase ~1.2 s uzoqda bo'lgani uchun
# xabarni keshlash (INSERT) tugagunicha bir necha sekund o'tadi.  Agar
# suhbatdosh xabarni YUBORIB DARHOL o'chirsa, o'chirish yangilamasi kesh
# yozuvidan OLDIN tekshiriladi va xabar "keshda yo'q" bo'lib tuyuladi —
# hisobot JIM o'tib ketadi (ovozli xabar / dumaloq video / rasm...).
#
# Shu sababli mazmun HECH QANDAY await'siz, xabarni qabul qilishning ENG
# BIRINCHI qadamida xotiradagi lug'atga yoziladi.  O'chirish hisoboti
# shu yozuvdan foydalanadi — DB yozuvi esa odatdagidek (statistika va
# qayta ishga tushishdan keyin ham ishlashi uchun) fonda davom etadi.
#
# Xotira chegaralangan: eng eski yozuvlar chiqib ketadi (DB — asosiy kesh).
# ---------------------------------------------------------------------------
INSTANT_CACHE_MAX = 5_000

# TTL: yozuv shu sekunddan keyin "eskirgan" hisoblanadi va tozalanadi.
# 0 = TTL o'chirilgan (faqat o'lcham chegarasi ishlaydi — eski xatti-harakat).
INSTANT_CACHE_TTL_SECONDS = 6 * 60 * 60.0

# Legacy tests/callers may still use (chat_id, message_id). Real business
# updates include connection_id and use (connection_id, chat_id, message_id),
# preventing two Telegram Business connections from sharing cached content.
_instant_cache: dict[tuple, dict] = {}


def _instant_key(
    chat_id: Optional[int],
    message_id: Optional[int],
    connection_id: Optional[str] = None,
) -> Optional[tuple]:
    if not chat_id or not message_id:
        return None
    if connection_id:
        return (str(connection_id), int(chat_id), int(message_id))
    return (int(chat_id), int(message_id))


def remember(
    chat_id: Optional[int],
    message_id: Optional[int],
    event_type: str,
    details: str,
    *,
    sender_id: Optional[int] = None,
    chat_title: str = UNKNOWN_CHAT,
    connection_id: Optional[str] = None,
) -> None:
    """Xabar mazmunini XOTIRAGA DARHOL yozadi (await YO'Q, I/O YO'Q).

    Bu funksiya ataylab sinxron: chaqiruvchi handler'ning birinchi qadamida
    ishlaydi, shuning uchun tez o'chirilgan xabar ham hisobotdan qolmaydi.
    """
    key = _instant_key(chat_id, message_id, connection_id)
    if key is None:
        return
    # Qayta yozilsa — yangi vaqt bilan yangilanadi (TTL qayta boshlanadi).
    _instant_cache.pop(key, None)
    _instant_cache[key] = {
        "event_type": event_type,
        "details": details,
        "sender_id": sender_id,
        "chat_title": chat_title,
        "stored_at": time.monotonic(),
        "stored_iso": now_iso(),
    }
    while len(_instant_cache) > INSTANT_CACHE_MAX:
        oldest = next(iter(_instant_cache))
        _instant_cache.pop(oldest, None)


def recall(
    chat_id: Optional[int],
    message_id: Optional[int],
    *,
    connection_id: Optional[str] = None,
) -> Optional[dict]:
    """Xotiradagi keshlangan mazmun (topilmasa yoki eskirgan bo'lsa ``None``)."""
    key = _instant_key(chat_id, message_id, connection_id)
    if key is None:
        return None
    entry = _instant_cache.get(key)
    if entry is None:
        return None
    if _expired(entry):
        _instant_cache.pop(key, None)
        return None
    return entry


def _expired(entry: dict, now: Optional[float] = None) -> bool:
    """Yozuv TTL bo'yicha eskirganmi?"""
    if INSTANT_CACHE_TTL_SECONDS <= 0:
        return False
    stamp = entry.get("stored_at")
    if stamp is None:
        return False
    return ((now or time.monotonic()) - float(stamp)) > INSTANT_CACHE_TTL_SECONDS


def purge_expired() -> int:
    """TTL bo'yicha eskirgan yozuvlarni o'chiradi. Qaytaradi: o'chirilganlar soni.

    Watchdog davriy ravishda chaqiradi; ``remember`` ham o'zi tozalab turadi.
    """
    if INSTANT_CACHE_TTL_SECONDS <= 0:
        return 0
    now = time.monotonic()
    stale = [key for key, entry in _instant_cache.items() if _expired(entry, now)]
    for key in stale:
        _instant_cache.pop(key, None)
    return len(stale)


def cache_stats() -> dict:
    """Admin panel "📦 Cache" ekrani uchun to'liq holat."""
    now = time.monotonic()
    entries = list(_instant_cache.values())
    count = len(entries)
    memory = sum(
        len(str(e.get("details") or "")) + len(str(e.get("chat_title") or "")) + 96
        for e in entries
    )
    oldest_age: Optional[int] = None
    oldest_iso = "—"
    if entries:
        oldest = min(entries, key=lambda e: e.get("stored_at", now))
        oldest_age = int(now - float(oldest.get("stored_at", now)))
        oldest_iso = str(oldest.get("stored_iso") or "—")
    usage = (count / INSTANT_CACHE_MAX * 100) if INSTANT_CACHE_MAX else 0.0
    if usage >= 95:
        status = "🔴 Full"
    elif usage >= 80:
        status = "⚠️ Near limit"
    else:
        status = "🟢 Healthy"
    return {
        "count": count,
        "max": INSTANT_CACHE_MAX,
        "usage_pct": usage,
        "memory_bytes": memory,
        "oldest_age": oldest_age,
        "oldest_iso": oldest_iso,
        "ttl_seconds": int(INSTANT_CACHE_TTL_SECONDS),
        "status": status,
        "mismatches": REPORTER_STATS["cache_db_mismatch"],
    }


def clear_instant_cache() -> None:
    """Tezkor keshni tozalash (testlar / qayta yuklash uchun)."""
    _instant_cache.clear()


# ---------------------------------------------------------------------------
# TAKRORIY HODISALARDAN HIMOYA (idempotentlik)
#
# Telegram bir xil update'ni qayta yuborishi mumkin (poll xatosi, ikki nusxa,
# qayta ishlash).  Bir xil tahrir/o'chirish/ulanish hodisasi IKKI MARTA
# hisobot qilinmasligi kerak.
# ---------------------------------------------------------------------------
_seen_events: dict[tuple, float] = {}


def _mark_seen(kind: str, *parts: Any) -> bool:
    """Hodisani "ko'rilgan" deb belgilaydi.

    Qaytaradi ``True`` — bu TAKROR (hisobot yuborilmaydi).
    """
    now = time.monotonic()
    # Eskirganlarni tozalash (chegaralangan xotira).
    if len(_seen_events) > INSTANT_CACHE_MAX:
        for key, stamp in list(_seen_events.items()):
            if now - stamp > DEDUPE_TTL_SECONDS:
                _seen_events.pop(key, None)
        while len(_seen_events) > INSTANT_CACHE_MAX:
            _seen_events.pop(next(iter(_seen_events)), None)
    key = (kind, *parts)
    stamp = _seen_events.get(key)
    if stamp is not None and now - stamp < DEDUPE_TTL_SECONDS:
        REPORTER_STATS["duplicates_suppressed"] += 1
        return True
    _seen_events[key] = now
    return False


def clear_seen_events() -> None:
    """Dedupe keshlarini tozalash (testlar uchun).

    Ikki keshni ham bo'shatadi: hodisa dedupe (``_seen_events``) va ulanish
    HOLATI dedupe (``_connection_states``).
    """
    _seen_events.clear()
    _connection_states.clear()


# ---------------------------------------------------------------------------
# ULANGAN HOLATI DEDUPLIKATSIYASI (state-aware)
#
# Ilgari ulanish hodisasi (business_connection) (connection_id, is_enabled)
# JUFTLIGI bo'yicha yutilardi.  Bu XATO edi: ``enabled -> disabled ->
# enabled`` ketma-ketligida oxirgi ``enabled`` update'i DEDUPE_TTL_SECONDS
# (10 daqiqa) ichida kelib qolsa, u "bir xil holat qayta keldi" deb
# noto'g'ri tashlab yuborilardi va ulanish o'chirilgan holatda qolib
# ketardi (haqiqiy holat o'zgarishi YO'QOLADI).
#
# Endi HAR BIR ulanish uchun OXIRGI ISHLANGAN holat eslab qolinadi:
#   * kiruvchi holat oxirgi ishlangan holat bilan BIR XIL  -> takroriy update
#     (yutiladi — qayta ishlanmaydi);
#   * kiruvchi holat BOSHQAChA                              -> HAQIQIY holat
#     o'zgarishi (DARHOL ishlanadi, hech qanday TTL kutmaydi).
#
# TTL faqat xotira chegarasi (eski yozuvlarni tozalash) uchun qoladi —
# holat o'zgarishini yutish uchun EMAS.
# ---------------------------------------------------------------------------
_connection_states: dict[str, tuple[bool, float]] = {}


def mark_connection_state(connection_id: str, is_enabled: bool) -> bool:
    """Ulanish hodisasi TAKRORmi? (state-aware dedupe)

    Qaytaradi:
        ``True``  — bir xil holat allaqachon ishlangan (takroriy update:
                    hisobot/DB yangilanishi qayta bajarilmasin);
        ``False`` — holat YANGI yoki O'ZGARGAN (darhol ishlanishi kerak).
    """
    if not connection_id:
        return False
    now = time.monotonic()
    state = bool(is_enabled)
    # Xotira chegarasi: eski yozuvlarni tozalash.
    if len(_connection_states) > INSTANT_CACHE_MAX:
        for key, (_value, stamp) in list(_connection_states.items()):
            if now - stamp > DEDUPE_TTL_SECONDS:
                _connection_states.pop(key, None)
        while len(_connection_states) > INSTANT_CACHE_MAX:
            _connection_states.pop(next(iter(_connection_states)), None)
    prev = _connection_states.get(connection_id)
    # TAKROR: shu ulanish uchun AYNAN shu holat yaqinda ishlangan.
    if prev is not None and prev[0] == state and now - prev[1] < DEDUPE_TTL_SECONDS:
        REPORTER_STATS["duplicates_suppressed"] += 1
        return True
    # YANGI / O'ZGARGAN holat — darhol ishlanadi.
    _connection_states[connection_id] = (state, now)
    return False


def clear_connection_states() -> None:
    """Ulanish holati dedupe keshini tozalash (testlar/restart uchun)."""
    _connection_states.clear()


def invalidate_user(user_id: int) -> None:
    """Foydalanuvchi o'chirilganda unga tegishli process-cache yozuvlarini tozalaydi."""
    uid = int(user_id or 0)
    _user_cache.pop(uid, None)
    for key in list(_connection_cache):
        entry = _connection_cache.get(key)
        if entry and entry[1] == uid:
            _connection_cache.pop(key, None)


class Reporter:
    """Faoliyat hisobotlarini ULANISH EGASIGA yetkazadi (qoidalar yuqorida)."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._owner_id: Optional[int] = None
        self._owner_chat: Optional[int] = None

    # ------------------------------------------------------------------ API

    async def report_incoming(self, message: Message) -> None:
        """business_message — hisobot YO'Q, tarkib faqat KESHlanadi.

        Ban/maintenance holati AVVAL tekshiriladi; bloklangan foydalanuvchi
        kontenti hatto tezkor keshga ham yozilmaydi. Keyin remember() await'siz
        ishlaydi, shuning uchun darhol kelgan delete/edit update'lari ham
        mazmunni topa oladi.
        """
        # 1) AVVAL connection owner + ban holatini aniqlaymiz.
        #    Banned owner uchun hech qanday cache/store ishlamasligi kerak.
        owner_id = await self._activate(message.business_connection_id)
        if owner_id is None:
            return

        # 2) ENG BIRINCHI QADAM, AWAIT'SIZ: mazmunni xotiraga yozamiz.
        #    aiogram update'larni parallel bajaradi — suhbatdosh xabarni yuborib
        #    DARHOL o'chirsa, o'chirish yangilamasi DB yozuvidan oldin kelishi
        #    mumkin; instant cache shu oynani yopadi.
        event_type, details = self._content_of(message)
        remember(
            message.chat.id if message.chat else None,
            message.message_id,
            event_type,
            details,
            sender_id=message.from_user.id if message.from_user else None,
            chat_title=self._chat_name(message),
            connection_id=message.business_connection_id,
        )

        # 3) DBga (asosiy kesh) yozish — statistika va restart uchun.
        # Takroriy update (poll qayta urinishi / ikki nusxa) — IKKI MARTA
        # yozilmasin (idempotentlik).
        chat_id = message.chat.id if message.chat else 0
        if _mark_seen(
            "incoming", message.business_connection_id, chat_id, message.message_id
        ):
            logger.info(
                "Takroriy business_message tashlab yuborildi (chat=%s mid=%s)",
                chat_id,
                message.message_id,
            )
            return
        await self._store(message, event_type, details)
        REPORTER_STATS["received"] += 1
        await self._log_activity(
            "business_message",
            f"Yangi xabar: {KIND_LABELS.get(event_type, event_type)}",
            connection_id=message.business_connection_id,
            user_id=owner_id,
            severity="INFO",
        )

    async def report_edited(self, message: Message, *, notify: bool = True) -> None:
        """edited_business_message — faqat suhbatdosh MATN tahriri haqida.

        * Egasining o'z tahriri           -> jim (kesh yangilanadi).
        * Media tahriri (izoh o'zgarishi) -> jim (kesh yangilanadi) — media
          haqida faqat O'CHIRILGANDA xabar beriladi.
        * Suhbatdosh matn tahriri         -> ✏️ hisobot (📱 Default → 📲 Edited).
        """
        owner_id = await self._activate(message.business_connection_id)
        if owner_id is None:
            return

        chat_id = message.chat.id if message.chat else 0
        # Takroriy tahrir update'i — ikkinchi marta hisobot qilinmasin.
        if _mark_seen(
            "edit", message.business_connection_id, chat_id, message.message_id
        ):
            logger.info(
                "Takroriy edited_business_message tashlab yuborildi "
                "(chat=%s mid=%s)",
                chat_id,
                message.message_id,
            )
            return

        stored = await db.get_event_by_message(
            chat_id, message.message_id, message.business_connection_id
        )
        # Xotiradagi tezkor kesh ham manba bo'la oladi (yozuv hali bazaga
        # tushmagan bo'lsa — "darhol tahrirlash" holati).
        record = stored or recall(
            chat_id, message.message_id,
            connection_id=message.business_connection_id,
        )
        if stored is None and record is not None:
            self._log_mismatch("edit", chat_id, message.message_id,
                              "xotirada bor, bazada hali yo'q (yozuv tugamagan)")
        # ESKI mazmun SHU YERDA o'qib olinadi: quyida kesh yangilanadi.
        old_text = (
            self._plain_content(record.get("details") or "") if record else NO_TEXT
        )
        is_media = self._has_media(message)
        # MATN tahririmi yoki IZOH (caption) tahririmi — aniq ajratamiz.
        is_caption_edit = message.text is None and message.caption is not None
        if is_media:
            old_content = self._media_caption(record.get("details") or "") if record else None
        else:
            old_content = old_text

        # 1) Keshni har doim yangilaymiz — keyingi o'chirish hisoboti shunga
        #    tayanadi (media izohining o'zgarishi ham shu yerda qamrab olinadi).
        if is_media:
            event_type, file_id, caption, file_name = self._current_media(message)
            if event_type and file_id:
                details = self._media_details(
                    file_id, event_type, caption, file_name
                )
                if stored:
                    await db.update_event_details(
                        chat_id, message.message_id, details,
                        message.business_connection_id,
                    )
                    # Tezkor keshni ham yangilaymiz — DB bilan sinxronlash.
                    # Agar kesh yangilanmasa, darhol o'chirishda recall() eski
                    # ma'lumotni qaytaradi (noto'g'ri event_type / file_id).
                    remember(
                        chat_id, message.message_id, event_type, details,
                        sender_id=stored.get("sender_id"),
                        chat_title=self._chat_name(message),
                        connection_id=message.business_connection_id,
                    )
                else:
                    await self._store(message, event_type, details)
        else:
            fresh = message.text or message.caption or NO_TEXT
            if stored:
                await db.update_event_details(
                    chat_id, message.message_id, fresh,
                    message.business_connection_id,
                )
                # Tezkor keshni ham yangilaymiz — DB bilan sinxronlash.
                remember(
                    chat_id, message.message_id, EVENT_TEXT, fresh,
                    sender_id=stored.get("sender_id"),
                    chat_title=self._chat_name(message),
                    connection_id=message.business_connection_id,
                )
            else:
                await self._store(message, EVENT_TEXT, fresh)

        # Statistika yozuvi (DB uchun — hisobot EMAS).
        #
        # TALAB: ESKI va YANGI matn/izoh IKKALASI ham saqlanadi — shu
        # sababli bitta qatorda "eski -> yangi" ko'rinishida yozamiz
        # (media izohi o'zgargan bo'lsa "caption" deb belgilanadi).
        new_content = message.text or message.caption or "media"
        kind_label = "caption" if (is_media or is_caption_edit) else "text"
        old_part = (old_content or NO_TEXT).replace("\n", " ")[:160]
        new_part = str(new_content).replace("\n", " ")[:160]
        await self._store_stat(
            message, EVENT_EDIT,
            f"{kind_label}: {old_part} -> {new_part}",
        )
        REPORTER_STATS["edited"] += 1
        await self._log_activity(
            "message_edited",
            f"Xabar tahrirlandi ({kind_label})",
            connection_id=message.business_connection_id,
            user_id=owner_id,
            severity="INFO",
        )

        # 2) Hisobot — faqat suhbatdoshning MATN tahriri.
        #    Eganing O'Z tahriri ham, ADMINning o'z tahriri ham JIM — xuddi
        #    o'chirishdagi kabi bir xil filtr (ilgari admin tekshiruvi faqat
        #    o'chirishda bor edi, natijada admin o'z xabarini tahrirlasa
        #    hisobot sizib chiqardi).
        sender = message.from_user
        if is_media or sender is None:
            return
        if sender.id == owner_id or _is_panel_admin(sender.id):
            logger.info(
                "Tahrir hisoboti o'tkazildi: yuboruvchi ega/admin "
                "(chat=%s mid=%s sender=%s)",
                chat_id, message.message_id, sender.id,
            )
            return
        new_text = message.text or message.caption or NO_TEXT
        body = REPORT_EDIT.format(
            who=self._who_from_user(sender),
            old=self._clip(old_text),
            new=self._clip(new_text),
        )
        if notify:
            await self._send(
                self._owner_chat, body + self._footer(self._chat_name(message))
            )

    # -- yordamchilar -------------------------------------------------------

    @staticmethod
    def _log_mismatch(kind: str, chat_id: int, message_id: int, reason: str) -> None:
        """Kesh va DB o'rtasidagi nomuvofiqlikni LOG qiladi (talab)."""
        REPORTER_STATS["cache_db_mismatch"] += 1
        logger.warning(
            "Cache/DB nomuvofiqligi (%s, chat=%s mid=%s): %s",
            kind, chat_id, message_id, reason,
        )

    @staticmethod
    async def _log_activity(
        event_type: str,
        description: str,
        *,
        connection_id: Optional[str] = None,
        user_id: Optional[int] = None,
        severity: str = "INFO",
    ) -> None:
        """Activity logga yozadi — DB xatosi hisobotni to'xtatmasligi kerak."""
        try:
            await db.add_activity_log(
                event_type, description,
                connection_id=connection_id, user_id=user_id, severity=severity,
            )
        except Exception:  # noqa: BLE001 – jurnal yozilmasa ham davom
            logger.debug("Activity log yozilmadi", exc_info=True)

    async def _warn_uncached(
        self, chat_title: str, missed: list[int], deleted_hms: str
    ) -> None:
        """Keshda topilmagan o'chirishlar haqida ogohlantiradi (15 daqiqada 1).

        Bu holatda hisobot UMUMAN chiqmaydi — jim qolsa, foydalanuvchi
        "ovoz/dumaloq video qaytmadi" deb ko'radi, sababi esa ko'rinmaydi.
        Shuning uchun sabab aytiladi: xabarni boshqa nusxa (eski build) qabul
        qilgan yoki xabar bot ishga tushishidan oldin yuborilgan.
        """
        global _last_uncached_warning
        if not missed or not self._owner_chat:
            return
        now = time.monotonic()
        if _last_uncached_warning and now - _last_uncached_warning < UNCACHED_WARNING_INTERVAL:
            return  # shovqin qilmaymiz
        _last_uncached_warning = now
        await self._send(
            self._owner_chat,
            REPORT_UNCACHED.format(
                chat=esc(chat_title),
                ids=", ".join(str(m) for m in missed[:MAX_BULK_DELETES]),
                count=len(missed),
                time=deleted_hms,
            ),
        )

    async def report_deleted(self, deleted: BusinessMessagesDeleted, *, notify: bool = True) -> None:
        """deleted_business_messages — suhbatdosh NIMA o'chirganini ko'rsatish.

        Har bir o'chirilgan xabar uchun:
        * matn bo'lsa   -> to'liq ASL MATN chiqariladi;
        * media bo'lsa  -> keshlangan fayl QAYTA YUBORILADI;
        * eganing o'z xabari yoki keshda yo'q id — JIM o'tkazib yuboriladi.
        """
        # O'CHIRILISH VAQTI — BITTA MARTA, update kelgan ZAHOTI olinadi.
        #
        # Telegram "deleted_business_messages" yangilamasini darhol yuboradi
        # va unda vaqt maydoni YO'Q (business_connection_id, chat, message_ids
        # — tamom).  Shuning uchun eng aniq manba — shu update qabul qilingan
        # payt.  Uni quyidagi DB so'rovlaridan OLDIN o'lchaymiz (ular Supabase
        # bilan ~1-3 sekund olishi mumkin) va butun hisobot uchun ishlatamiz:
        # sarlavha, media izohi va "Chat/Vaqt" qatori — hammasi AYNAN bir
        # vaqtni ko'rsatadi (sekin fayl yuklash ham uni surib ketmaydi).
        deleted_hms = hms(now_report())

        owner_id = await self._activate(deleted.business_connection_id)
        if owner_id is None:
            return

        chat = deleted.chat
        chat_id = chat.id if chat else 0
        chat_title = self._chat_title_of(chat)

        # Ommaviy o'chirishda (bulk delete) chegaradan ko'p id kelsa —
        # hisobotlar soni cheklanadi, qolgani LOG qilinadi va egasiga bitta
        # qisqa eslatma yuboriladi (jim yo'qolib qolmaydi).
        bulk_ids = list(deleted.message_ids or [])
        truncated = 0
        if len(bulk_ids) > MAX_BULK_DELETES:
            truncated = len(bulk_ids) - MAX_BULK_DELETES
            REPORTER_STATS["bulk_truncated"] += 1
            logger.warning(
                "Bulk delete: %d ta id keldi, faqat %d tasi hisobot qilinadi "
                "(chat=%s)",
                len(bulk_ids), MAX_BULK_DELETES, chat_id,
            )
        missed: list[int] = []
        for mid in bulk_ids[:MAX_BULK_DELETES]:
            # Takroriy delete update'i (Telegram qayta yuborishi mumkin) —
            # bir xil xabar uchun IKKI MARTA hisobot chiqmasin.
            if _mark_seen("delete", deleted.business_connection_id, chat_id, mid):
                logger.info(
                    "Takroriy delete tashlab yuborildi (chat=%s mid=%s)",
                    chat_id, mid,
                )
                continue

            stored = await db.get_event_by_message(
                chat_id, mid, deleted.business_connection_id
            )
            # Baza yozuvi hali tugamagan bo'lsa (tez o'chirish) — xotiradagi
            # tezkor keshlga tayanamiz; aks holda xabar "keshda yo'q" bo'lib
            # ko'rinib, hisobot umuman kelmasdi.
            cached_record = recall(
                chat_id, mid, connection_id=deleted.business_connection_id
            )
            record = stored or cached_record
            if stored is None and record is not None:
                self._log_mismatch(
                    "delete", chat_id, mid,
                    "xotirada bor, bazada hali yo'q (yozuv tugamagan)",
                )
            elif stored is not None and cached_record is None:
                # DBda bor, xotirada yo'q — restart/TTL natijasi: hisobot DBdan
                # tiklanadi (ma'lumot yo'qolmaydi), lekin sabab log qilinadi.
                self._log_mismatch(
                    "delete", chat_id, mid,
                    "bazada bor, xotirada yo'q (restart yoki TTL)",
                )

            if not record:
                # Keshda yo'q: BOSHQA nusxa qabul qilgan yoki xabar bot ishga
                # tushishidan oldin yuborilgan.  Bu JIM o'tkazib yuborilmaydi:
                # aks holda "ovoz/dumaloq video kelmadi" ning sababi ko'rinmaydi.
                logger.warning(
                    "Delete skipped: message %s is not cached (chat=%s)",
                    mid,
                    chat_id,
                )
                REPORTER_STATS["uncached_deletes"] += 1
                await self._log_activity(
                    "delete_uncached",
                    f"O'chirilgan xabar keshda topilmadi (mid={mid}, chat={chat_id})",
                    connection_id=deleted.business_connection_id,
                    user_id=owner_id,
                    severity="WARNING",
                )
                missed.append(mid)
                continue
            sender_id = record.get("sender_id")
            # Eganing o'z xabari yoki admin o'chirgan istalgan xabar — hisobot YO'Q
            if sender_id and (
                int(sender_id) == owner_id
                or _is_panel_admin(int(sender_id))  # env + runtime admin rollari
            ):
                # Nega hisobot yo'qligi logda ko'rinib tursin (jim bo'shliq
                # qolmasin): o'chirilgan xabar EGANING yoki ADMINning o'zi
                # yuborgan xabari.
                logger.info(
                    "Delete hisoboti o'tkazildi: yuboruvchi ega/admin "
                    "(chat=%s mid=%s sender=%s)",
                    chat_id, mid, sender_id,
                )
                continue  # hisobot YO'Q (talab)

            event_type = record.get("event_type") or EVENT_TEXT
            details = record.get("details") or ""
            label = KIND_LABELS.get(event_type, KIND_LABELS[EVENT_TEXT])
            who = await self._who_for_delete(chat, record)

            if event_type == EVENT_TEXT:
                # 1) MATN: to'liq asl matn ko'rsatiladi.
                body = REPORT_DELETED_TEXT.format(
                    who=who, original=self._clip(self._plain_content(details))
                )
                if notify:
                    await self._send(
                        self._owner_chat,
                        body + self._footer(chat_title, deleted_at=deleted_hms),
                    )
            elif event_type in MEDIA_EVENTS:
                # 2) MEDIA: keshlangan faylni QAYTA YUBORAMIZ.
                #    Natija: (yuborildimi, asl ko'rinish rad etilish sababi).
                #    Ovozli xabar / dumaloq videoni Telegram BA'ZAN rad etadi
                #    (qabul qiluvchining maxfiylik sozlamasi), shuning uchun
                #    rad etilsa baytlar yuklab olinib FAYL sifatida yuboriladi.
                file_id = self._extract_file_id(details)
                file_name = self._media_file_name(details)
                delivered = False
                reason: Optional[str] = None
                if notify and file_id:
                    # Katta fayllar RAMga TO'LIQ yuklanmaydi — oqim bilan
                    # vaqtincha faylga tushiriladi (server resursi himoyasi).
                    delivered, reason = await self._resend_media(
                        self._owner_chat, event_type, file_id,
                        header=REPORT_DELETED_MEDIA_CAPTION.format(
                            kind=label, who=who
                        ),
                        chat_title=chat_title,
                        caption=self._media_caption(details),
                        file_name=file_name,
                        deleted_at=deleted_hms,
                    )
                if notify and not delivered:
                    body = REPORT_DELETED_MEDIA.format(kind=label, who=who, mid=mid)
                    if reason:
                        body += REPORT_RESEND_FAILED.format(reason=esc(reason))
                    await self._send(
                        self._owner_chat,
                        body + self._footer(chat_title, deleted_at=deleted_hms),
                    )
            elif event_type == EVENT_OTHER:
                # 3) BOSHQA kontent (so'rovnoma, joylashuv, kontakt, o'yin...):
                #    media fayli yo'q — qisqa kartochka (jim yo'qolib qolmasin).
                body = REPORT_DELETED_MEDIA.format(kind=label, who=who, mid=mid)
                if notify:
                    await self._send(
                        self._owner_chat,
                        body + self._footer(chat_title, deleted_at=deleted_hms),
                    )
            else:
                continue

            # Statistika yozuvi (DB uchun — hisobot EMAS).
            await db.add_event(
                user_id=owner_id,
                event_type=(
                    EVENT_DELETE_MEDIA if event_type in MEDIA_EVENTS else EVENT_DELETE
                ),
                details=f"deleted {label.lower()}: {details[:100]}",
                chat_id=chat.id if chat else None,
                chat_title=chat_title,
                message_id=None,  # asl yozuvni "soyabon" qilmasin
                business_connection_id=deleted.business_connection_id,
            )
            REPORTER_STATS["deleted_reports"] += 1
            await self._log_activity(
                "message_deleted",
                f"O'chirilgan xabar hisoboti: {label}",
                connection_id=deleted.business_connection_id,
                user_id=owner_id,
                severity="INFO",
            )

        # Keshda topilmagan id bo'lsa — faqat reportlar ochiq paytda ogohlantiramiz.
        if notify:
            await self._warn_uncached(chat_title, missed, deleted_hms)

        if truncated:
            if notify:
                await self._send(
                    self._owner_chat,
                    f"{E_TRASH} <b>Ommaviy o'chirish</b>\n\nYana {truncated} ta xabar "
                    f"o'chirildi (hisobot chegarasi {MAX_BULK_DELETES} ta).",
                )
            await self._log_activity(
                "delete_bulk_truncated",
                f"Bulk delete: {truncated} ta xabar hisobotga kirmadi",
                connection_id=deleted.business_connection_id,
                user_id=owner_id,
                severity="WARNING",
            )

    # ------------------------------------------------------------ internals

    async def _activate(self, connection_id: Optional[str]) -> Optional[int]:
        """Ulanishni tekshiradi va egasini keshlaydi.

        Ulanish faol bo'lsa — eganing user_id qaytariladi (hisobot manzili
        ham keshlanadi).  Aks holda None: na kesh, na hisobot.  Har bir
        'yo'q' sababi LOG qilinadi.

        Natija PROTSESS bo'ylab (_connection_cache) eslab qolinadi — bir xil
        ulanish uchun DBga qayta murojaat qilinmaydi.  Kesh faqat
        ``invalidate_connection`` (business_connection hodisasi) yoki TTL
        orqali yangilanadi.
        """
        if not connection_id:
            logger.warning("Reporter: business_connection_id bo'sh — update o'tdi")
            return None

        # 1) Kesh: shu ulanish yaqinda o'qilgan bo'lsa DBga bormaymiz.
        now = time.monotonic()
        cached = _connection_cache.get(connection_id)
        if cached is not None and now - cached[0] < _CONNECTION_TTL_SECONDS:
            self._owner_id = cached[1]
            self._owner_chat = cached[2]
            try:
                from app.services import moderation
                if moderation.is_banned(self._owner_id):
                    logger.info("Reporter: banned owner %s uchun report to'xtatildi", self._owner_id)
                    return None
            except Exception:
                logger.debug("Reporter ban check failed", exc_info=True)
            return self._owner_id

        # 2) Keshda yo'q (yoki TTL o'tdi) — DBdan o'qiymiz.
        conn = await db.get_connection(connection_id)
        if not conn:
            logger.warning("Reporter: ulanish DBda topilmadi (%s)", connection_id)
            return None
        if not conn.get("is_enabled"):
            logger.info("Reporter: ulanish o'chirilgan (%s)", connection_id)
            return None
        self._owner_id = int(conn["user_id"])
        try:
            from app.services import moderation
            if moderation.is_banned(self._owner_id):
                logger.info("Reporter: banned owner %s uchun report to'xtatildi", self._owner_id)
                return None
        except Exception:
            logger.debug("Reporter ban check failed", exc_info=True)
        chat_id = conn.get("user_chat_id") or conn.get("user_id")
        self._owner_chat = int(chat_id) if chat_id else None
        # Faqat FAOL ulanish keshlanadi — uzilgani qayta o'qiladi.
        _connection_cache[connection_id] = (now, self._owner_id, self._owner_chat)
        return self._owner_id

    # -- tarkib kesh formati -----------------------------------------------
    # matn :  to'liq matn (escape QILINMAGAN xom holda)
    # media:  "file:<file_id>|<event_type>|<caption>"

    @staticmethod
    def _media_details(
        file_id: str,
        event_type: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> str:
        # Format: "file:<file_id>|<event_type>|<caption>|<file_name>".
        # 4-maydon (file_name) — musiqa/fayl uchun ASL nom (qayta yuborishda
        # ishlatiladi).  Eski yozuvlarda u bo'lmasligi mumkin, shuning uchun
        # o'quvchilar toq sonli maydonlarga ham chidamli.
        # ``|`` belgisiz saqlaymiz — ajratish (parsing) bir xil bo'lishi uchun.
        cap = (caption or "").replace("|", " ")
        name = (file_name or "").replace("|", " ")
        return f"file:{file_id}|{event_type}|{cap}|{name}"

    @staticmethod
    def _extract_file_id(details: str) -> Optional[str]:
        """'file:<id>|...' dan file_id ni oladi."""
        if details.startswith("file:"):
            payload = details[5:]
            return payload.split("|", 1)[0] or None
        return None

    @staticmethod
    def _media_caption(details: str) -> Optional[str]:
        """Keshlangan media izohi (caption) — 3-maydon."""
        parts = details.split("|", 3)
        if len(parts) >= 3 and parts[2]:
            return parts[2]
        return None

    @staticmethod
    def _media_file_name(details: str) -> Optional[str]:
        """Keshlangan ASL fayl nomi — 4-maydon (bo'lmasa ``None``)."""
        parts = details.split("|", 3)
        if len(parts) >= 4 and parts[3]:
            return parts[3]
        return None

    @staticmethod
    def _plain_content(details: str) -> str:
        """Matn yozuvidan xom matnni oladi (escape holda emas)."""
        return details

    def _content_of(self, message: Message) -> tuple[str, str]:
        """Xabar TURI va keshlanadigan mazmuni (SOF SINXRON — await yo'q).

        * media -> ``("photo", "file:<file_id>|photo|<izoh>")`` va h.k.
        * matn  -> ``("text", "<asl matn>")``

        Tartib ``report_incoming`` bilan bir xil: stiker, GIF, rasm, video,
        ovozli xabar, dumaloq video, keyin matn.
        """
        if message.sticker:
            return EVENT_STICKER, self._media_details(
                message.sticker.file_id, EVENT_STICKER
            )
        if message.animation:  # GIF
            return EVENT_ANIMATION, self._media_details(
                message.animation.file_id, EVENT_ANIMATION, message.caption
            )
        if message.photo:
            return EVENT_PHOTO, self._media_details(
                message.photo[-1].file_id, EVENT_PHOTO, message.caption
            )
        if message.video:
            return EVENT_VIDEO, self._media_details(
                message.video.file_id, EVENT_VIDEO, message.caption
            )
        if message.voice:
            return EVENT_VOICE, self._media_details(
                message.voice.file_id, EVENT_VOICE, message.caption
            )
        if message.video_note:  # dumaloq (circular) video
            return EVENT_VIDEO_NOTE, self._media_details(
                message.video_note.file_id, EVENT_VIDEO_NOTE
            )
        audio = getattr(message, "audio", None)  # musiqa / audio fayl
        if audio:
            return EVENT_AUDIO, self._media_details(
                audio.file_id, EVENT_AUDIO, message.caption,
                getattr(audio, "file_name", None),
            )
        document = getattr(message, "document", None)  # har qanday fayl
        if document:
            return EVENT_DOCUMENT, self._media_details(
                document.file_id, EVENT_DOCUMENT, message.caption,
                getattr(document, "file_name", None),
            )
        # CATCH-ALL: tanib bo'lmagan yozma kontent (so'rovnoma, joylashuv,
        # kontakt, o'yin/dice va h.k.) ham keshlanadi — o'chirilganda jim
        # qolib ketmasligi uchun ("yozilgan, yuborilgan, o'chirilgan"
        # hammasi botga yetib borishi kerak).
        content_type = getattr(message, "content_type", None)
        if content_type and content_type != "text":
            return EVENT_OTHER, esc(str(content_type))
        return EVENT_TEXT, (message.text or message.caption or NO_TEXT)

    @staticmethod
    def _has_media(message: Message) -> bool:
        return bool(
            message.sticker
            or message.animation
            or message.photo
            or message.video
            or message.voice
            or message.video_note
            or getattr(message, "audio", None)
            or getattr(message, "document", None)
        )

    def _current_media(
        self, message: Message
    ) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """Tahrirlangan xabardagi joriy media -> (tur, file_id, izoh, fayl nomi)."""
        if message.sticker:
            return EVENT_STICKER, message.sticker.file_id, None, None
        if message.animation:
            return (
                EVENT_ANIMATION,
                message.animation.file_id,
                message.caption,
                None,
            )
        if message.photo:
            return EVENT_PHOTO, message.photo[-1].file_id, message.caption, None
        if message.video:
            return EVENT_VIDEO, message.video.file_id, message.caption, None
        if message.voice:
            return EVENT_VOICE, message.voice.file_id, message.caption, None
        if message.video_note:
            return EVENT_VIDEO_NOTE, message.video_note.file_id, None, None
        audio = getattr(message, "audio", None)
        if audio:
            return (
                EVENT_AUDIO,
                audio.file_id,
                message.caption,
                getattr(audio, "file_name", None),
            )
        document = getattr(message, "document", None)
        if document:
            return (
                EVENT_DOCUMENT,
                document.file_id,
                message.caption,
                getattr(document, "file_name", None),
            )
        return None, None, None, None

    # -- "Kim" va chat nomlari ------------------------------------------------

    def _who_from_user(self, user: TgUser) -> str:
        """Hisobotdagi 'Who' — USERNAME ustun: @username, bo'lmasa ism."""
        if user is None:
            return WHO_UNKNOWN
        if user.username:
            return f"@{user.username}"
        name = user.first_name or str(user.id)
        return mention_by_id(user.id, name)

    async def _who_for_delete(self, chat: Optional[Chat], stored: dict) -> str:
        """O'chirilgan xabar KIMniki — username afzal ko'riladi.

        Suhbatdoshlar bot bilan /start qilmaydi, shuning uchun ular users
        jadvalida bo'lmasligi mumkin — unda chat ma'lumotidan foydalanamiz.
        """
        sender_id = stored.get("sender_id")
        if sender_id:
            user = await self._user_cached(int(sender_id))
            if user:
                username = user.get("username")
                if username:
                    return f"@{username}"
                name = user.get("first_name") or str(sender_id)
                return mention_by_id(int(sender_id), name, username)
        if chat is not None and chat.type == "private" and chat.username:
            return f"@{chat.username}"
        if chat is not None:
            raw = chat.first_name or chat.title
            if raw:
                return esc(raw[:MAX_TITLE])
        title = stored.get("chat_title")
        if title:
            return esc(title[:MAX_TITLE])
        return WHO_UNKNOWN

    @staticmethod
    def _chat_title_of(chat: Optional[Chat]) -> str:
        if chat is None:
            return UNKNOWN_CHAT
        raw = chat.title or chat.first_name or chat.username
        return esc(raw[:MAX_TITLE]) if raw else UNKNOWN_CHAT

    @staticmethod
    def _chat_name(message: Message) -> str:
        if message.chat is None:
            return UNKNOWN_CHAT
        title = message.chat.title or message.chat.first_name or message.chat.username
        return esc(title[:MAX_TITLE]) if title else UNKNOWN_CHAT

    # -- yuborish ------------------------------------------------------------

    def _footer(self, chat_title: str, *, deleted_at: Optional[str] = None) -> str:
        """Hisobot oxiridagi "Chat + Vaqt" qatori.

        ``deleted_at`` berilgan bo'lsa — qator O'CHIRILGAN vaqtni ko'rsatadi
        (o'chirish hisoboti uchun).  Aks holda hozirgi vaqt (tahrirlash
        hisoboti).  Ikkalasi ham Toshkent (UTC+5) vaqtida.
        """
        if deleted_at is not None:
            return REPORT_FOOTER_DELETED.format(chat=chat_title, time=deleted_at)
        return REPORT_FOOTER.format(chat=chat_title, time=hms(now_report()))

    async def _resend_media(
        self,
        chat_id: Optional[int],
        event_type: str,
        file_id: str,
        *,
        header: str,
        chat_title: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        deleted_at: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        """Saqlangan media faylni qayta yuborish (o'chirilganda).

        Qaytaradi: ``(yuborildimi, asl ko'rinish rad etilish sababi)``.

        * ``(True, None)``  — fayl ASL ko'rinishida ketdi (file_id bilan);
        * ``(True, sabab)`` — asl ko'rinish rad etildi, lekin baytlar yuklab
          olinib FAYL sifatida yuborildi (foydalanuvchi mazmunni ko'radi);
        * ``(False, sabab)`` — umuman yuborilmadi, chaqiruvchi matnli
          zaxira variantni yuboradi va sababni ko'rsatadi.
        """
        if chat_id is None:
            return False, None
        cap = self._apply_gap(header)
        if caption:
            cap += f"\n{E_CHAT} Caption: {self._clip(caption)}"
        cap += self._footer(chat_title, deleted_at=deleted_at)

        try:
            await self._send_typed(chat_id, event_type, file_id, cap)
            return True, None
        except Exception as exc:  # noqa: BLE001 – file_id muddati / cheklov
            reason = self._reason_of(exc)
            # SABAB KO'RINADIGAN bo'lishi shart: jim yutilsa "ovozli xabar
            # qaytmadi" ning sababini hech qachon bilmaymiz.
            logger.error(
                "Resend refused (type=%s, file=%s...): %s",
                event_type,
                file_id[:12],
                reason,
                exc_info=True,
            )

        # 2) Zaxira yo'l: faylni yuklab, FAYL sifatida yuboramiz.
        #    "Ovozli xabar" va "dumaloq video" ko'rinishini Telegram qabul
        #    qiluvchining sozlamasiga qarab rad etishi mumkin — oddiy fayl
        #    (document / audio) esa har doim o'tadi.
        #
        #    KATTA FAYLLAR: RAMni to'ldirib qo'ymaslik uchun oqim (stream)
        #    bilan vaqtincha FAYLGA yuklanadi va yuborilgach DARHOL o'chiriladi.
        #    Bir vaqtda yuklanadigan/o'chiriladigan fayllar soni
        #    :mod:`app.utils.telegram_api` dagi umumiy CONCURRENCY limit bilan
        #    cheklanadi (bir nechta katta video birga o'chirilsa ham server
        #    ostidan ketmaydi).
        cap += REPORT_RESEND_FILE_FORM.format(reason=esc(reason))
        if VOICE_PRIVACY_MARKER in reason and _voice_hint_due():
            # Sabab foydalanuvchining O'Z Telegram sozlamasi — qaysi birini
            # ochish kerakligini aytib qo'yamiz (bir soatda bir marta).
            cap += REPORT_VOICE_SETTING_HINT
        temp_path: Optional[str] = None
        try:
            data, temp_path, too_large = await self._fetch_media(file_id)
            if too_large:
                logger.warning(
                    "Media yuborilmadi (type=%s): %s", event_type, too_large
                )
                return False, too_large
            await self._send_as_file(
                chat_id, event_type, cap, reason=reason,
                data=data, temp_path=temp_path, file_name=file_name,
            )
            REPORTER_STATS["media_file_fallback"] += 1
            return True, reason
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "File fallback failed (type=%s): %s",
                event_type,
                self._reason_of(exc),
                exc_info=True,
            )
            return False, reason
        finally:
            # CLEANUP: vaqtincha fayl HAR QANDAY holatda o'chiriladi.
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    logger.debug("Vaqtinchalik faylni o'chirib bo'lmadi: %s", temp_path)

    async def _send_typed(
        self, chat_id: int, event_type: str, file_id: str, cap: str
    ) -> None:
        """Faylni ASL ko'rinishida yuboradi (xato bo'lsa — ko'taradi)."""
        if event_type == EVENT_STICKER:
            await tg_call(
                "send_sticker", lambda: self.bot.send_sticker(chat_id, sticker=file_id)
            )
            # Stikerlarga caption yozib bo'lmaydi — izoh alohida ketadi.
            await self._send(chat_id, cap)
        elif event_type == EVENT_PHOTO:
            await tg_call(
                "send_photo",
                lambda: self.bot.send_photo(
                    chat_id, photo=file_id, caption=cap, parse_mode="HTML"
                ),
            )
        elif event_type == EVENT_VIDEO:
            await tg_call(
                "send_video",
                lambda: self.bot.send_video(
                    chat_id, video=file_id, caption=cap, parse_mode="HTML"
                ),
            )
        elif event_type == EVENT_ANIMATION:
            await tg_call(
                "send_animation",
                lambda: self.bot.send_animation(
                    chat_id, animation=file_id, caption=cap, parse_mode="HTML"
                ),
            )
        elif event_type == EVENT_VOICE:
            await tg_call(
                "send_voice",
                lambda: self.bot.send_voice(
                    chat_id, voice=file_id, caption=cap, parse_mode="HTML"
                ),
            )
        elif event_type == EVENT_VIDEO_NOTE:
            # Dumaloq videoga caption yozib bo'lmaydi — izoh alohida ketadi.
            await tg_call(
                "send_video_note",
                lambda: self.bot.send_video_note(chat_id, video_note=file_id),
            )
            await self._send(chat_id, cap)
        elif event_type in (EVENT_AUDIO, EVENT_DOCUMENT):
            # Musiqa (audio) va fayl (document) — ASL FAYL ko'rinishida qayta
            # yuboriladi (talab: "musiqa bo'lsa fayl, fayl bo'lsa fayl").
            await tg_call(
                "send_document",
                lambda: self.bot.send_document(
                    chat_id, document=file_id, caption=cap, parse_mode="HTML"
                ),
            )
        else:
            raise ValueError(f"noma'lum media turi: {event_type}")

    async def _file_size(self, file_id: str) -> Optional[int]:
        """Fayl o'lchamini aniqlaydi (katta fayllarni RAMga olmaslik uchun)."""
        try:
            info = await tg_call(
                "get_file", lambda: self.bot.get_file(file_id),
                attempts=2, timeout=30.0,
            )
        except Exception as exc:  # noqa: BLE001 – o'lcham noma'lum bo'lsa
            logger.info(
                "Fayl o'lchami aniqlanmadi (%s): %s",
                type(exc).__name__, self._reason_of(exc),
            )
            return None
        size = getattr(info, "file_size", None)
        return int(size) if size else None

    async def _fetch_media(
        self, file_id: str
    ) -> tuple[Optional[bytes], Optional[str], Optional[str]]:
        """Faylni yuklab oladi: kichigi xotiraga, KATTASI vaqtincha faylga.

        Qaytaradi: ``(data, temp_path, too_large_sababi)``.
        * ``data`` — xotiradagi baytlar (kichik fayl);
        * ``temp_path`` — vaqtincha fayl yo'li (katta fayl; chaqiruvchi
          o'chirishi SHART);
        * ``too_large`` — fayl ochib bo'lmas darajada katta (yuklanmadi).
        """
        size = await self._file_size(file_id)
        if size is not None and size > MEDIA_MAX_DOWNLOAD:
            REPORTER_STATS["media_too_large"] += 1
            return None, None, (
                f"fayl juda katta ({size // (1024 * 1024)} MB, "
                f"limit {MEDIA_MAX_DOWNLOAD // (1024 * 1024)} MB)"
            )

        if size is not None and size > MEDIA_MEMORY_LIMIT:
            fd, path = tempfile.mkstemp(prefix="botmedia_", suffix=".bin")
            os.close(fd)

            def _dst() -> Any:
                return open(path, "wb")

            try:
                with _dst() as fp:
                    await tg_call(
                        "download",
                        lambda: self.bot.download(file_id, destination=fp),
                        attempts=2, timeout=MEDIA_DOWNLOAD_TIMEOUT,
                    )
            except Exception:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                raise
            if os.path.getsize(path) == 0:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                raise RuntimeError("fayl bo'sh")
            return None, path, None

        buffer = await tg_call(
            "download", lambda: self.bot.download(file_id),
            attempts=2, timeout=MEDIA_DOWNLOAD_TIMEOUT,
        )
        if buffer is None:
            raise RuntimeError("fayl yuklab olinmadi")
        data = buffer.getvalue() if hasattr(buffer, "getvalue") else buffer.read()
        if not data:
            raise RuntimeError("fayl bo'sh")
        return data, None, None

    async def _send_as_file(
        self,
        chat_id: int,
        event_type: str,
        cap: str,
        *,
        reason: Optional[str] = None,
        data: Optional[bytes] = None,
        temp_path: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> None:
        """Baytlarni FAYL ko'rinishida yuboradi (asl ko'rinish rad etilganda).

        Ovozli xabar -> audio (ijro etiladi), dumaloq video -> video, musiqa va
        fayl -> document (ASL nomi bilan).  Har bir qadam xato bersa, eng oxirgi
        chora document.

        ``reason`` — Telegram bergan rad javobi.  Unda ovozli-xabar maxfiyligi
        (VOICE_MESSAGES_FORBIDDEN) bo'lsa, audio ko'rinishlarini umuman
        sinab o'tirmaymiz (ular baribir rad etiladi) — to'g'ridan-to'g'ri
        neytral nomli fayl yuboriladi.
        """
        muted = bool(reason) and VOICE_PRIVACY_MARKER in reason
        # Musiqa/fayl uchun ASL nom saqlangan bo'lsa — o'shani ishlatamiz
        # (foydalanuvchi faylni taniy olsin).  Yo'ldan chiqish belgilarisiz.
        name = (
            os.path.basename(file_name)
            if file_name
            else FILE_NAMES.get(event_type, DEFAULT_FILE_NAME)
        )
        if muted and event_type == EVENT_VOICE:
            name = VOICE_BLOCKED_FILE_NAME

        def upload() -> Any:
            # Har bir urinish uchun YANGI obyekt (oqim qayta ishlatilmaydi).
            if temp_path:
                return FSInputFile(temp_path, filename=name)
            return BufferedInputFile(data or b"", filename=name)

        if event_type == EVENT_VOICE and not muted:
            try:
                await tg_call(
                    "send_audio",
                    lambda: self.bot.send_audio(
                        chat_id, audio=upload(), caption=cap, parse_mode="HTML"
                    ),
                )
                return
            except Exception:  # noqa: BLE001
                logger.info("Audio sifatida ham yuborilmadi — document")
        elif event_type == EVENT_VIDEO_NOTE:
            # Dumaloq video oddiy VIDEO sifatida o'tadi (maxfiylik sozlamasi
            # ovozli xabarlarga tegishli, videoga emas) — tekshirilgan.
            try:
                await tg_call(
                    "send_video_fallback",
                    lambda: self.bot.send_video(
                        chat_id, video=upload(), caption=cap, parse_mode="HTML"
                    ),
                )
                return
            except Exception:  # noqa: BLE001
                logger.info("Video sifatida ham yuborilmadi — document")

        if event_type == EVENT_STICKER:
            # Stikerga izoh yozilmaydi — u alohida xabar bo'lib ketadi.
            await tg_call(
                "send_document",
                lambda: self.bot.send_document(chat_id, document=upload()),
            )
            await self._send(chat_id, cap)
            return
        await tg_call(
            "send_document",
            lambda: self.bot.send_document(
                chat_id, document=upload(), caption=cap, parse_mode="HTML"
            ),
        )

    @staticmethod
    def _reason_of(exc: BaseException) -> str:
        """Xato matnidan QISQA sabab (Telegram izohi) ajratib oladi."""
        raw = getattr(exc, "message", None) or str(exc)
        text = " ".join(str(raw).split())
        for prefix in (
            "Telegram server says - ",
            "Bad Request: ",
            "Forbidden: ",
            "Conflict: ",
        ):
            if text.startswith(prefix):
                text = text[len(prefix):]
        return (text or exc.__class__.__name__)[:140]

    @staticmethod
    def _apply_gap(html: str) -> str:
        """Sarlavha bilan asosiy qatorlar ORASIDAGI masofani sozlaydi.

        Har bir hisobot shabloni sarlavhadan keyin aynan bitta "\n\n"
        (bitta bo'sh qator) bilan boshlanadi — birinchi uchraganni
        ``settings.report_line_gap`` ta bo'sh qatorga almashtiramiz:
            1 -> hozirgi ko'rinish, 0 -> yopiq, 2 -> kengroq.
        """
        gap_count = max(0, int(settings.report_line_gap))
        return html.replace("\n\n", "\n" * (gap_count + 1), 1)

    async def _send(self, chat_id: Optional[int], html: str) -> None:
        """Hisobotni yuboradi (429/backoff — :mod:`app.utils.telegram_api`)."""
        if chat_id is None:
            logger.warning("Reporter: hisobot manzili yo'q — yuborilmadi")
            return
        html = self._apply_gap(html)
        try:
            await tg_call(
                "send_message",
                lambda: self.bot.send_message(
                    chat_id,
                    html,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                ),
                attempts=3,
            )
        except Exception as exc:  # noqa: BLE001 – hisobot yuborilmasa bot to'xtamaydi
            logger.info(
                "Could not deliver report to chat %s: %s",
                chat_id,
                self._reason_of(exc),
            )

    async def _user_cached(self, user_id: int) -> Optional[dict]:
        """Foydalanuvchi yozuvini TTL kesh bilan o'qiydi (N+1 oldini oladi).

        Ommaviy o'chirishda bir xil suhbatdosh uchun o'nlab marta DBga
        murojaat qilinmasligi kerak.
        """
        now = time.monotonic()
        cached = _user_cache.get(user_id)
        if cached is not None and now - cached[0] < _USER_CACHE_TTL_SECONDS:
            return cached[1]
        try:
            user = await db.get_user(user_id)
        except Exception:  # noqa: BLE001 – DB xatosi hisobotni to'xtatmasin
            logger.debug("get_user xatosi (user=%s)", user_id, exc_info=True)
            return None
        _user_cache[user_id] = (now, user)
        if len(_user_cache) > _USER_CACHE_MAX:
            _user_cache.pop(next(iter(_user_cache)), None)
        return user

    # -- DB yozuvlari ----------------------------------------------------------

    async def _store(
        self, message: Message, event_type: str, details: str
    ) -> None:
        """Xabar yozuvini KESHlash (chat_id + message_id + sender_id bilan).

        sender_id — aynan kim yubordi (ega yoki suhbatdosh): o'chirilganda
        egasining o'z xabarini JIM o'tkazib yuborish uchun kerak.
        """
        await db.add_event(
            user_id=self._owner_id or 0,
            event_type=event_type,
            details=details,
            chat_id=message.chat.id if message.chat else None,
            chat_title=self._chat_name(message),
            message_id=message.message_id,
            sender_id=message.from_user.id if message.from_user else None,
            business_connection_id=message.business_connection_id,
        )
        # Tezkor keshni ham bir xil holatga keltiramiz (manba bitta bo'lsin).
        remember(
            message.chat.id if message.chat else None,
            message.message_id,
            event_type,
            details,
            sender_id=message.from_user.id if message.from_user else None,
            chat_title=self._chat_name(message),
            connection_id=message.business_connection_id,
        )

    async def _store_stat(
        self, message: Message, event_type: str, details: str
    ) -> None:
        """Faqat statistika yozuvi (message_id YO'Q — keshni soyabon qilmasin)."""
        await db.add_event(
            user_id=self._owner_id or 0,
            event_type=event_type,
            details=details,
            chat_id=message.chat.id if message.chat else None,
            chat_title=self._chat_name(message),
            message_id=None,
            sender_id=message.from_user.id if message.from_user else None,
            business_connection_id=message.business_connection_id,
        )

    # ---------------------------------------------------------- kichik util

    @staticmethod
    def _clip(text: str) -> str:
        text = esc(text)
        if len(text) <= MAX_TEXT:
            return text
        return text[:MAX_TEXT] + TRUNCATED
