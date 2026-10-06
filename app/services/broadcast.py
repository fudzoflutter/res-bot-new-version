"""
Broadcast / reklama dvigateli (Ad Builder + yuborish + progress).

NEGA SHU MODUL
--------------
Broadcast ALLAQACHON mavjud edi (``app/web.py`` ichida) va u 202/409,
retry (429/network), xatolarni izolyatsiya qilish bilan ishlardi.  Shu sababli
u QAYTA YOZILMADI — KENGAYTIRILDI:

* reklama (ad) modeli: photo/video + caption + 1..10 URL tugma (har birida
  matn, URL va STYLE) — eski ``Matn - https://link`` formati ham ishlaydi;
* preview va test-send (test yuborish broadcast statistikasiga TEGMAYDI);
* progress endi BAZADA (``broadcast_recipients``) — PENDING / SENDING / SENT /
  FAILED / UNKNOWN holatlari saqlanadi. UNKNOWN ataylab Retry Failed'ga kiritilmaydi,
  chunki Telegram tarmoq uzilishida xabar qabul qilingan bo'lishi mumkin;
* oluvchilar BO'LAKLARDA o'qiladi — 30 000 foydalanuvchi RAMga yuklanmaydi.

ESKI API SAQLANADI: holat lug'ati (:data:`state`) ``app/web.py`` orqali
``_broadcast_state`` nomi bilan ham ko'rinadi, shuning uchun mavjud testlar va
status endpoint o'zgarishsiz ishlaydi.
"""

from __future__ import annotations

from app.emoji_config import EMOJI

import asyncio
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import settings
from app.utils.telegram_api import call as tg_call

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konstantalar
# ---------------------------------------------------------------------------
MAX_BUTTONS = 10
BUTTON_STYLES = ("primary", "success", "danger")

MEDIA_PHOTO = "photo"
MEDIA_VIDEO = "video"
MEDIA_ANIMATION = "animation"
MEDIA_TEXT = "text"

BROADCAST_STATE_KEY = "broadcast_state"
BROADCAST_STATE_VERSION = 2
LAST_BROADCAST_KEY = "broadcast_last_id"

PERSIST_INTERVAL = 2.0        # sekund — har bir oluvchida bazaga yozmaslik uchun
READ_BATCH = 500              # bir marta o'qiladigan oluvchilar soni (RAM chegarasi)
FAILED_SAMPLE = 100           # status endpoint ko'rsatadigan xato olganlar


def _send_delay() -> float:
    """Yuborishlar orasidagi minimal pauza (Telegram rate limitiga hurmat).

    ``BROADCAST_SEND_DELAY`` env orqali o'zgartiriladi (testlarda 0).
    """
    raw = os.getenv("BROADCAST_SEND_DELAY", "0.05").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.05


class AdError(ValueError):
    """Noto'g'ri reklama (HTTP 400 ga aylanadi)."""


class BroadcastBusy(RuntimeError):
    """Bir vaqtda faqat bitta broadcast (HTTP 409 ga aylanadi)."""


# ---------------------------------------------------------------------------
# Reklama modeli
# ---------------------------------------------------------------------------
@dataclass
class AdButton:
    text: str
    url: str
    style: str = ""


@dataclass
class Ad:
    """Yuboriladigan reklama: media (photo/video) + caption + tugmalar."""

    caption: str = ""
    media_url: str = ""
    media_type: str = MEDIA_TEXT
    buttons: list[AdButton] = field(default_factory=list)

    @property
    def has_media(self) -> bool:
        return bool(self.media_url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "caption": self.caption,
            "media_url": self.media_url if self.media_url.lower().startswith(("http://", "https://")) else "",
            "media_file_id": self.media_url if not self.media_url.lower().startswith(("http://", "https://")) else "",
            "media_type": self.media_type,
            "buttons": [
                {"text": b.text, "url": b.url, "style": b.style} for b in self.buttons
            ],
        }


def media_kind(url: str, declared: str = "") -> str:
    """Media turini aniqlaydi (aniq ko'rsatilgan bo'lsa — o'sha)."""
    value = (declared or "").strip().lower()
    if value in (MEDIA_PHOTO, MEDIA_VIDEO, MEDIA_ANIMATION):
        return value
    lower = (url or "").lower()
    if lower.endswith((".mp4", ".mov", ".avi", ".mkv", ".webm")):
        return MEDIA_VIDEO
    if lower.endswith(".gif"):
        return MEDIA_ANIMATION
    return MEDIA_PHOTO


_URL_START = re.compile(r"https?://", re.IGNORECASE)


def _split_button_line(line: str) -> tuple[str, str]:
    """``Matn - https://...`` qatorini (matn, url) ga ajratadi.

    URL ichidagi ``-`` (masalan ``https://my-site.uz``) va matn ichidagi
    defis (``Bir-ikki - https://...``) noto'g'ri kesilmasligi uchun
    ajratuvchi sifatida ``http(s)://`` dan OLDINGI oxirgi ``-`` olinadi.
    """
    line = line.strip()
    match = _URL_START.search(line)
    if not match:
        text, _, url = line.partition("-")
        return text.strip(), url.strip()
    url = line[match.start():].strip()
    head = line[: match.start()].rstrip()
    if head.endswith(("-", "–", "—")):
        head = head[:-1]
    return head.strip(), url


def parse_buttons(raw: Any) -> list[AdButton]:
    """Tugmalarni o'qiy oladigan formatlarga aylantiradi.

    Qabul qilinadi:

    * tuzilgan ro'yxat: ``[{"text": "Kanal", "url": "https://...", "style": "primary"}]``
    * qatorlar: ``["Kanal - https://...", ...]``
    * bitta matn (eski format): ``"Kanal - https://...\\nSayt - https://..."``
    """
    items: list[Any] = []
    if isinstance(raw, str):
        items = [line for line in raw.split("\n") if line.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    elif raw:
        raise AdError("Tugmalar formati noto'g'ri.")

    buttons: list[AdButton] = []
    for item in items:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("label") or "").strip()
            url = str(item.get("url") or item.get("link") or "").strip()
            style = str(item.get("style") or "").strip().lower()
        else:
            text, url = _split_button_line(str(item))
            style = ""
        if not text and not url:
            continue
        if not text or not url:
            raise AdError("Har bir tugmada matn va URL bo'lishi shart.")
        if not url.lower().startswith(("http://", "https://")):
            raise AdError(f"Tugma URL http(s):// bilan boshlanishi kerak: {url[:60]}")
        if style and style not in BUTTON_STYLES:
            raise AdError(
                f"Tugma stili noto'g'ri: {style} ({'/'.join(BUTTON_STYLES)})."
            )
        if len(text) > 64:
            raise AdError("Tugma matni 64 belgidan oshmasin.")
        buttons.append(AdButton(text=text, url=url, style=style))

    if len(buttons) > MAX_BUTTONS:
        raise AdError(
            f"Tugmalar soni {MAX_BUTTONS} tadan oshmasin (berildi: {len(buttons)})."
        )
    return buttons


def build_ad(payload: dict[str, Any]) -> Ad:
    """Payload'dan tekshirilgan :class:`Ad` yasaydi (xato -> :class:`AdError`)."""
    if not isinstance(payload, dict):
        raise AdError("So'rov formati noto'g'ri.")
    caption = str(payload.get("caption") or payload.get("text") or "").strip()
    media_url = str(
        payload.get("media_url") or payload.get("media") or payload.get("photo") or ""
    ).strip()
    file_id = str(payload.get("media_file_id") or "").strip()
    if file_id and not re.fullmatch(r"[A-Za-z0-9_-]{10,512}", file_id):
        raise AdError("Media file_id noto‘g‘ri.")
    if media_url and not media_url.lower().startswith(("http://", "https://")):
        raise AdError("Media URL http(s):// bilan boshlanishi kerak.")
    media_url = file_id or media_url
    if not caption and not media_url:
        raise AdError("Matn (caption) yoki media URL bo'lishi shart.")
    max_caption = 1024 if media_url else 4096
    if len(caption) > max_caption:
        raise AdError(f"Matn {max_caption} belgidan oshmasin.")
    buttons = parse_buttons(payload.get("buttons"))
    return Ad(
        caption=caption,
        media_url=media_url,
        media_type=media_kind(media_url, str(payload.get("media_type") or "")),
        buttons=buttons,
    )


def build_markup(ad: Ad):  # noqa: ANN201 – aiogram tipi (sikl importidan qochamiz)
    """Inline klaviaturani quradi (tugma bo'lmasa ``None``)."""
    if not ad.buttons:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list[InlineKeyboardButton]] = []
    for button in ad.buttons:
        payload: dict[str, Any] = {"text": button.text, "url": button.url}
        if button.style:
            payload["style"] = button.style
        rows.append([InlineKeyboardButton(**payload)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def preview(ad: Ad) -> dict[str, Any]:
    """Preview: yuboriladigan xabarning AYNAN tuzilishi (matn ko'rinishida)."""
    lines: list[str] = []
    if ad.has_media:
        lines.append(f"{EMOJI.preview_media.plain} MEDIA [{ad.media_type}]: {ad.media_url}")
    if ad.caption:
        lines.append(f"{EMOJI.report_text.plain} CAPTION:")
        lines.append(ad.caption)
    if ad.buttons:
        lines.append("")
        lines.append(f"{EMOJI.preview_buttons.plain} TUGMALAR ({len(ad.buttons)}/{MAX_BUTTONS}):")
        for index, button in enumerate(ad.buttons, start=1):
            style = f" [{button.style}]" if button.style else ""
            lines.append(f"  {index}. {button.text} -> {button.url}{style}")
    else:
        lines.append("")
        lines.append(f"{EMOJI.preview_buttons.plain} TUGMALAR: yo'q")
    return {
        **ad.to_dict(),
        "buttons_count": len(ad.buttons),
        "max_buttons": MAX_BUTTONS,
        "preview_text": "\n".join(lines),
    }


# ---------------------------------------------------------------------------
# Holat (web.py `_broadcast_state` nomi bilan ham ko'radi)
# ---------------------------------------------------------------------------
state: dict[str, Any] = {
    "running": False,
    "total": 0,
    "sent": 0,
    "failed": 0,
    "pending": 0,
    "unknown": 0,
    "started_at": None,
    "finished_at": None,
    "last_error": "",
    "interrupted": False,
    "broadcast_id": "",
    "mode": "none",       # broadcast | retry | none
    "requested_by": 0,
}

_task: Optional[asyncio.Task] = None
_last_persist = 0.0
# start()/retry_failed() ichida seeding (await) paytida ham "band" deb hisoblanadi.
# Flag await'dan OLDIN, sinxron o'rnatiladi — shuning uchun ikki parallel
# start() chaqiruvi ikkita broadcast yarata olmaydi (asyncio kooperativ).
_starting = False


def snapshot() -> dict[str, Any]:
    """Joriy holatning nusxasi (JSON uchun xavfsiz)."""
    return dict(state)


def is_running() -> bool:
    return bool(state["running"]) or _starting


def _serialize() -> str:
    payload = dict(state)
    payload["v"] = BROADCAST_STATE_VERSION
    return json.dumps(payload)


async def persist_state(*, force: bool = False) -> None:
    """Holatni bazaga yozadi (throttled; xatosi broadcastni to'xtatmaydi)."""
    global _last_persist
    now = time.monotonic()
    if not force and now - _last_persist < PERSIST_INTERVAL:
        return
    _last_persist = now
    payload = _serialize()  # snapshot await'dan OLDIN
    try:
        from app.database import db

        await db.set_setting(BROADCAST_STATE_KEY, payload)
    except Exception:  # noqa: BLE001
        logger.debug("Broadcast holatini saqlab bo'lmadi", exc_info=True)


async def recover_state() -> bool:
    """Restart/deploy dan keyin holatni tiklaydi.

    Qaytaradi ``True`` — bazada YARIM QOLGAN (``running``) broadcast topildi.

    MUHIM: tiklash HECH NARSA YUBORMAYDI (dublikat yuborishlar bo'lmasin).
    Yarim qolgan ``SENDING`` qatorlari "UNKNOWN" holatiga o'tadi. UNKNOWN
    qayta yuborilmaydi, chunki Telegram xabarni qabul qilgan bo'lishi mumkin.
    """
    global _last_persist
    from app.database import db

    try:
        raw = await db.get_setting(BROADCAST_STATE_KEY, "")
    except Exception:  # noqa: BLE001
        logger.debug("Broadcast holatini o'qib bo'lmadi", exc_info=True)
        return False
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("Broadcast holati buzilgan — e'tiborga olinmadi")
        return False
    if not isinstance(data, dict):
        return False

    def _as_int(name: str) -> int:
        try:
            return max(0, int(data.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    was_running = bool(data.get("running"))
    restored = {
        "running": False,
        "total": _as_int("total"),
        "sent": _as_int("sent"),
        "failed": _as_int("failed"),
        "unknown": _as_int("unknown"),
        "pending": _as_int("pending"),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "last_error": str(data.get("last_error") or "")[:200],
        "interrupted": was_running,
        "broadcast_id": str(data.get("broadcast_id") or ""),
        "mode": str(data.get("mode") or "none"),
        "requested_by": _as_int("requested_by"),
    }
    if was_running:
        restored["finished_at"] = time.time()
        restored["last_error"] = restored["last_error"] or "restart/deploy paytida uzildi"
        restored["pending"] = max(
            0, restored["total"] - restored["sent"] - restored["failed"]
        )
        logger.warning(
            "Broadcast restartdan keyin TIKLANDI (uzilgan): sent=%s failed=%s total=%s",
            restored["sent"], restored["failed"], restored["total"],
        )
    state.clear()
    state.update(restored)
    _last_persist = time.monotonic()

    # Yarim qolgan SENDING qatorlar — holat noma'lum (qayta yuborilmaydi).
    if restored["broadcast_id"]:
        try:
            stuck = await db.broadcast_ids(
                restored["broadcast_id"], "SENDING", limit=10_000
            )
            for user_id in stuck:
                await db.broadcast_mark(
                    restored["broadcast_id"], user_id, "UNKNOWN",
                    "restart paytida uzilib qoldi (Telegram qabul qilgan bo'lishi mumkin)",
                )
        except Exception:  # noqa: BLE001
            logger.debug("SENDING qatorlarni belgilash xatosi", exc_info=True)
    if restored["broadcast_id"]:
        try:
            counts = await db.broadcast_counts(restored["broadcast_id"])
            if counts:
                restored["total"] = int(counts.get("total") or restored["total"])
                restored["sent"] = int(counts.get("sent") or 0)
                restored["failed"] = int(counts.get("failed") or 0)
                restored["unknown"] = int(counts.get("unknown") or 0)
                restored["pending"] = int(counts.get("pending") or 0)
                state.update(restored)
        except Exception:
            logger.debug("Broadcast recovery counts o'qilmadi", exc_info=True)
    return was_running


# ---------------------------------------------------------------------------
# Yuborish
# ---------------------------------------------------------------------------
async def send_one(bot, user_id: int, ad: Ad, media_input=None):  # noqa: ANN001
    """Bitta foydalanuvchiga yuboradi (429/network — retry bilan)."""
    markup = build_markup(ad)
    media = media_input if media_input is not None else ad.media_url
    if not ad.has_media:
        factory = lambda: bot.send_message(  # noqa: E731
            user_id, ad.caption, parse_mode="HTML", reply_markup=markup,
            request_timeout=180 if media_input is not None else 60,
        )
    elif ad.media_type == MEDIA_VIDEO:
        factory = lambda: bot.send_video(  # noqa: E731
            user_id, video=media, caption=ad.caption or None,
            parse_mode="HTML", reply_markup=markup,
            request_timeout=180 if media_input is not None else 60,
        )
    elif ad.media_type == MEDIA_ANIMATION:
        factory = lambda: bot.send_animation(  # noqa: E731
            user_id, animation=media, caption=ad.caption or None,
            parse_mode="HTML", reply_markup=markup,
            request_timeout=180 if media_input is not None else 60,
        )
    else:
        factory = lambda: bot.send_photo(  # noqa: E731
            user_id, photo=media, caption=ad.caption or None,
            parse_mode="HTML", reply_markup=markup,
            request_timeout=180 if media_input is not None else 60,
        )
    return await tg_call("broadcast_send", factory, attempts=3, timeout=180 if media_input is not None else 60)


async def test_send(bot, ad: Ad, target_id: int) -> None:  # noqa: ANN001
    """Test yuborish — FAQAT ``target_id`` ga.

    Broadcast HOLATIGA, hisoblagichlariga va ``broadcast_recipients``
    jadvaliga TA'SIR QILMAYDI.
    """
    return await send_one(bot, int(target_id), ad)


# ---------------------------------------------------------------------------
# Oluvchilar
# ---------------------------------------------------------------------------
def _new_broadcast_id() -> str:
    return f"bc_{int(time.time())}_{random.randint(1000, 9999)}"


def _excluded_recipients() -> set[int]:
    """Ads must not be sent to admin-role holders or banned users."""
    from app.services import admin_roles, moderation

    excluded = set(admin_roles.list_admins())
    excluded.update(int(row["user_id"]) for row in moderation.list_banned())
    return excluded


async def count_recipients() -> int:
    """Current eligible ad-recipient count used by Preview.

    Preview ham real broadcast kabi bo'laklab o'qiydi.  Shunda userlar soni
    katta bo'lsa ham barcha IDlarni bir paytda RAMga yuklamaymiz va Preview
    bilan Send bir xil exclusion qoidalaridan foydalanadi.
    """
    from app.database import db

    excluded = _excluded_recipients()
    total = 0
    offset = 0
    while True:
        raw_chunk = await db.all_user_ids(limit=READ_BATCH, offset=offset)
        if not raw_chunk:
            break
        offset += len(raw_chunk)
        total += sum(1 for uid in raw_chunk if uid and uid not in excluded)
    return total


async def _seed_recipients(broadcast_id: str) -> int:
    """Oluvchilarni BO'LAKLAB yozadi va jami sonni qaytaradi."""
    from app.database import db

    excluded_recipients = _excluded_recipients()
    offset = 0
    while True:
        raw_chunk = await db.all_user_ids(limit=READ_BATCH, offset=offset)
        if not raw_chunk:
            break
        # Pagination RAW page bo'yicha yuradi. Aks holda bir sahifadagi barcha
        # userlar admin bo'lsa filtrlangan ``chunk`` bo'sh chiqib, keyingi
        # oddiy userlar umuman broadcast ro'yxatiga kirmay qolishi mumkin.
        offset += len(raw_chunk)
        chunk = [uid for uid in raw_chunk if uid and uid not in excluded_recipients]
        if chunk:
            await db.broadcast_seed(broadcast_id, chunk)
    counts = await db.broadcast_counts(broadcast_id)
    return int(counts.get("total") or 0)


async def _pending_ids(broadcast_id: str) -> list[int]:
    from app.database import db

    return await db.broadcast_ids(broadcast_id, "PENDING", limit=READ_BATCH)


def _is_uncertain_send_error(exc: BaseException) -> bool:
    """Telegram/network xatosi server qabul qilgan-qilmaganini noaniq qilishi mumkinmi?"""
    name = type(exc).__name__
    uncertain_names = {
        "TelegramNetworkError",
        "TelegramServerError",
        "TelegramRetryAfter",
        "TimeoutError",
        "ConnectionError",
        "ConnectionResetError",
        "BrokenPipeError",
        "OSError",
    }
    return name in uncertain_names or isinstance(exc, (TimeoutError, ConnectionError, OSError))


async def run_recipients(bot, ad: Ad, broadcast_id: str, total: int) -> None:  # noqa: ANN001
    """Barcha PENDING oluvchilarga yuboradi va har bir state'ni ishonchli saqlaydi.

    Definitiv Telegram xatosi -> FAILED (Retry Failed mumkin).
    Noaniq network/server xatosi -> UNKNOWN (Retry Failed qilinmaydi).
    Recipient uchun yakuniy DB state yozilmasa, job to'xtatiladi: aks holda
    state noma'lum qolgan userga dublikat yuborish xavfi tug'iladi.
    """
    from app.database import db

    sent = 0
    failed = 0
    unknown = 0
    last_error = ""
    delay = _send_delay()
    processed: set[int] = set()
    try:
        while True:
            pending = [
                user_id
                for user_id in await _pending_ids(broadcast_id)
                if user_id not in processed
            ]
            if not pending:
                break
            for user_id in pending:
                processed.add(user_id)
                if not state["running"]:
                    last_error = last_error or "to'xtatildi"
                    break

                marked_sending = False
                mark_error = ""
                for attempt in range(3):
                    try:
                        await db.broadcast_mark(broadcast_id, user_id, "SENDING")
                        marked_sending = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        mark_error = f"state persist: {type(exc).__name__}: {exc}"[:200]
                        if attempt < 2:
                            await asyncio.sleep(0.2 * (attempt + 1))
                if not marked_sending:
                    failed += 1
                    last_error = mark_error or "recipient state persist failed"
                    try:
                        await db.broadcast_mark(broadcast_id, user_id, "FAILED", last_error)
                    except Exception:
                        logger.error("Recipient FAILED state yozilmadi; broadcast to'xtatildi (user=%s)", user_id, exc_info=True)
                        state["running"] = False
                        state["last_error"] = last_error
                        break
                    state.update({
                        "sent": sent, "failed": failed, "unknown": unknown,
                        "pending": max(0, total - sent - failed - unknown),
                    })
                    await persist_state()
                    continue

                try:
                    await send_one(bot, user_id, ad)
                    sent += 1
                    status, error = "SENT", ""
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    error = f"{type(exc).__name__}: {exc}"[:200]
                    last_error = error
                    if _is_uncertain_send_error(exc):
                        unknown += 1
                        status = "UNKNOWN"
                        logger.warning("Broadcast send UNCERTAIN for %s: %s", user_id, exc)
                    else:
                        failed += 1
                        status = "FAILED"
                        logger.warning("Broadcast send failed for %s: %s", user_id, exc)

                persisted = False
                persist_error = ""
                for attempt in range(3):
                    try:
                        await db.broadcast_mark(broadcast_id, user_id, status, error)
                        persisted = True
                        break
                    except Exception as exc:
                        persist_error = f"final state persist: {type(exc).__name__}: {exc}"[:200]
                        if attempt < 2:
                            await asyncio.sleep(0.2 * (attempt + 1))
                if not persisted:
                    last_error = persist_error or "final recipient state persist failed"
                    state["running"] = False
                    state["last_error"] = last_error
                    logger.error(
                        "Broadcast final state saqlanmadi; duplicate xavfini oldini olish uchun job to'xtatildi (user=%s status=%s)",
                        user_id, status,
                    )
                    break

                state.update({
                    "sent": sent,
                    "failed": failed,
                    "unknown": unknown,
                    "pending": max(0, total - sent - failed - unknown),
                    "last_error": last_error,
                })
                await persist_state()
                if delay:
                    await asyncio.sleep(delay)
            if not state["running"]:
                break
    finally:
        state.update({
            "running": False,
            "sent": sent,
            "failed": failed,
            "unknown": unknown,
            "pending": max(0, total - sent - failed - unknown),
            "last_error": last_error,
            "finished_at": time.time(),
        })
        await persist_state(force=True)
        logger.info(
            "Broadcast finished. sent=%d failed=%d unknown=%d total=%d (id=%s)",
            sent, failed, unknown, total, broadcast_id,
        )
        try:
            from app.services.audit import log_action

            await log_action(
                "broadcast_finished",
                actor=int(state.get("requested_by") or 0),
                target=broadcast_id,
                result="ok" if not unknown else "uncertain",
                new=f"sent={sent} failed={failed} unknown={unknown} total={total}",
                severity="WARNING" if unknown else "INFO",
            )
        except Exception:  # noqa: BLE001
            logger.debug("broadcast audit yozilmadi", exc_info=True)


def _spawn(coro) -> None:  # noqa: ANN001
    global _task
    _task = asyncio.create_task(coro)


async def start(bot, ad: Ad, *, requested_by: int = 0) -> dict[str, Any]:  # noqa: ANN001
    """Yangi broadcast boshlaydi (fon vazifasida).

    Xatolar: :class:`BroadcastBusy` (allaqachon ishlayapti),
    :class:`AdError` (oluvchi yo'q).
    """
    global _starting
    # check-and-set orasida await YO'Q -> atomik (parallel start() ikkinchisi Busy oladi)
    if state["running"] or _starting:
        raise BroadcastBusy("Broadcast allaqachon ishlayapti")
    _starting = True
    try:
        from app.database import db

        broadcast_id = _new_broadcast_id()
        total = await _seed_recipients(broadcast_id)
        if total <= 0:
            raise AdError("Oluvchi topilmadi.")
        # Reklama snapshoti state=running dan OLDIN majburiy saqlanadi.
        # Snapshot saqlanmasa, running broadcast holati qolmaydi.
        await save_ad(broadcast_id, ad)
        state.update({
            "running": True,
            "total": total,
            "sent": 0,
            "failed": 0,
            "unknown": 0,
            "pending": total,
            "started_at": time.time(),
            "finished_at": None,
            "last_error": "",
            "interrupted": False,
            "broadcast_id": broadcast_id,
            "mode": "broadcast",
            "requested_by": int(requested_by or 0),
        })
        await persist_state(force=True)
        try:
            await db.set_setting(LAST_BROADCAST_KEY, broadcast_id)
        except Exception:  # noqa: BLE001
            logger.debug("last broadcast id yozilmadi", exc_info=True)
        _spawn(run_recipients(bot, ad, broadcast_id, total))
        return {"broadcast_id": broadcast_id, "total": total, "status": snapshot()}
    except BaseException:
        # Vazifa ishga tushmasdan xato bo'lsa — "running" qotib qolmasin.
        if _task is None or _task.done():
            state["running"] = False
        raise
    finally:
        _starting = False


async def retry_failed(bot, *, requested_by: int = 0) -> dict[str, Any]:  # noqa: ANN001
    """XATO olgan VA hali yuborilmagan (PENDING) oluvchilarga yuboradi.

    * FAILED -> PENDING ga qaytariladi va qayta uriniladi;
    * restart/to'xtatish tufayli hech qachon urinilmagan PENDING oluvchilar
      ham DAVOM ETTIRILADI (uzilgan broadcast tugatiladi);
    * muvaffaqiyatli yuborilganlar (SENT) TAKRORLANMAYDI.

    Eslatma: restart paytida ``SENDING`` holatida qolgan oluvchi UNKNOWN deb belgilanadi
    va Retry Failed unga tegmaydi. Bu Telegram so'rovi serverda qabul qilingan
    bo'lishi mumkinligi sababli dublikat yuborish xavfini kamaytiradi.
    """
    global _starting
    if state["running"] or _starting:
        raise BroadcastBusy("Broadcast allaqachon ishlayapti")
    _starting = True
    try:
        from app.database import db

        broadcast_id = str(state.get("broadcast_id") or "")
        if not broadcast_id:
            try:
                broadcast_id = await db.get_setting(LAST_BROADCAST_KEY, "")
            except Exception:  # noqa: BLE001
                broadcast_id = ""
        if not broadcast_id:
            raise AdError("Qayta yuborish uchun oldingi broadcast topilmadi.")

        # Bazani o'zgartirishdan OLDIN reklama mavjudligini tekshiramiz.
        ad = await _ad_from_setting(broadcast_id)
        if ad is None:
            raise AdError("Oldingi reklama matni topilmadi (qayta yuborib bo'lmaydi).")

        await db.broadcast_requeue_failed(broadcast_id)
        counts = await db.broadcast_counts(broadcast_id)
        to_send = int(counts.get("pending") or 0)
        if to_send <= 0:
            raise AdError(
                "Qayta yuborish uchun xato olgan yoki yuborilmay qolgan oluvchi yo'q."
            )

        state.update({
            "running": True,
            "total": to_send,
            "sent": 0,
            "failed": 0,
            "unknown": 0,
            "pending": to_send,
            "started_at": time.time(),
            "finished_at": None,
            "last_error": "",
            "interrupted": False,
            "broadcast_id": broadcast_id,
            "mode": "retry",
            "requested_by": int(requested_by or 0),
        })
        await persist_state(force=True)
        _spawn(run_recipients(bot, ad, broadcast_id, to_send))
        return {"broadcast_id": broadcast_id, "total": to_send, "status": snapshot()}
    except BaseException:
        if _task is None or _task.done():
            state["running"] = False
        raise
    finally:
        _starting = False


# ---------------------------------------------------------------------------
# Reklama matnini saqlash (Retry Failed uchun kerak)
# ---------------------------------------------------------------------------
AD_SETTING_PREFIX = "broadcast_ad:"


async def save_ad(broadcast_id: str, ad: Ad) -> None:
    """Broadcast boshlanishidan oldin reklama snapshotini DBga majburiy saqlaydi."""
    from app.database import db

    try:
        await db.set_setting(f"{AD_SETTING_PREFIX}{broadcast_id}", json.dumps(ad.to_dict()))
        # Immediately read it back so Retry Failed uchun snapshot haqiqatan mavjudligi tasdiqlanadi.
        saved = await db.get_setting(f"{AD_SETTING_PREFIX}{broadcast_id}", "")
        if not saved:
            raise RuntimeError("advertisement snapshot was not persisted")
    except Exception as exc:  # noqa: BLE001
        logger.error("Reklama snapshoti saqlanmadi (id=%s)", broadcast_id, exc_info=True)
        raise AdError("Reklama snapshotini bazaga saqlab bo'lmadi; broadcast boshlanmadi.") from exc


async def _ad_from_setting(broadcast_id: str) -> Optional[Ad]:
    from app.database import db

    try:
        raw = await db.get_setting(f"{AD_SETTING_PREFIX}{broadcast_id}", "")
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return build_ad(data)
    except AdError:
        logger.warning("Saqlangan reklama o'qilmadi (id=%s)", broadcast_id)
        return None


async def status_payload() -> dict[str, Any]:
    """Status endpoint uchun: holat + bazadagi aniq hisoblar + xato olganlar."""
    from app.database import db

    payload = snapshot()
    broadcast_id = str(payload.get("broadcast_id") or "")
    if broadcast_id:
        try:
            counts = await db.broadcast_counts(broadcast_id)
        except Exception:  # noqa: BLE001
            logger.debug("broadcast_counts xatosi", exc_info=True)
            counts = {}
        if counts:
            payload["counts"] = counts
            payload["unknown"] = counts.get("unknown", payload.get("unknown", 0))
            if not payload["running"]:
                payload["sent"] = counts.get("sent", payload["sent"])
                payload["failed"] = counts.get("failed", payload["failed"])
                payload["pending"] = counts.get("pending", payload["pending"])
                payload["total"] = counts.get("total", payload["total"])
        try:
            failed_ids = await db.broadcast_ids(
                broadcast_id, "FAILED", limit=FAILED_SAMPLE
            )
            if failed_ids:
                payload["failed_sample"] = failed_ids
        except Exception:  # noqa: BLE001
            logger.debug("failed sample o'qilmadi", exc_info=True)
    return payload
