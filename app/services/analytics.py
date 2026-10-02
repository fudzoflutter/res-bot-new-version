"""
Analytics — HAQIQIY baza ma'lumotlari ustida hisoblanadigan ko'rsatkichlar.

QOIDALAR
--------
* Hech qanday QATTIQ RAQAM / demo / tasodifiy qiymat YO'Q — barcha sonlar
  ``users``, ``connections``, ``events`` jadvallaridan SQL agregatsiya bilan
  olinadi (``db.analytics_overview`` / ``db.analytics_series``).
* Hisoblash BITTA (yoki bir nechta) parametrlangan so'rovda bajariladi —
  30 000 foydalanuvchini RAMga yuklash yo'q.
* TAKRORIY HODISALAR IKKI MARTA SANALMAYDI: Telegram update'lari kirishda
  (:mod:`app.services.reporter`) deduplikatsiya qilinadi, analytics esa
  ``events`` jadvalidagi yozuvlarni bir marta sanaydi.
* VAQT: baza vaqtni MAHALLIY ISO-8601 matn sifatida saqlaydi
  (:func:`app.utils.timeutils.now_iso`, ``local_now()``).  Shu sababli
  barcha chegaralar ham xuddi shu soatda (naive local) hisoblanadi —
  aralash mintaqa (aware/naive) taqqoslash xatosi bo'lmaydi.
  Ko'rsatish uchun mintaqa ``timezone`` maydonida qaytariladi
  (hisobot mintaqasi, :mod:`app.utils.timeutils`).

TA'RIFLAR (loyihadagi mavjud model asosida)
-------------------------------------------
* **Active users** — shu oynada ``users.last_activity`` bo'lgan YOKI shu
  oynada biznes-hodisasi (``events``) bo'lgan noyob foydalanuvchilar.
  Eslatma: bot faqat OXIRGI faollikni saqlaydi, shuning uchun o'tgan
  oraliqda bot bilan yozishgan-u, keyin yana faol bo'lgan foydalanuvchi
  faqat hodisalari (events) bo'lsagina hisobga olinadi.
* **Messages** — ``edit``/``delete``/``delete_media``/``connection``
  bo'lmagan hodisalar (ya'ni qabul qilingan xabarlar).
* **Deleted** — ``delete`` + ``delete_media``.
* **Edited** — ``edit``.
* **Media** — sticker/photo/video/animation/voice/video_note/audio/document.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from app.utils.timeutils import local_now

logger = logging.getLogger(__name__)

RANGE_24H = "24h"
RANGE_7D = "7d"
RANGE_30D = "30d"
RANGE_CUSTOM = "custom"
VALID_RANGES: tuple[str, ...] = (RANGE_24H, RANGE_7D, RANGE_30D, RANGE_CUSTOM)
DEFAULT_RANGE = RANGE_24H

#: Bitta so'rovda so'ralishi mumkin bo'lgan eng katta oraliq (kun).
MAX_RANGE_DAYS = 366

#: Standart oraliqlar: nom -> (kun, bucket).
_PRESETS: dict[str, dict[str, Any]] = {
    RANGE_24H: {"days": 1, "bucket": "hour"},
    RANGE_7D: {"days": 7, "bucket": "day"},
    RANGE_30D: {"days": 30, "bucket": "day"},
}

#: Seriya nuqtalarining eng katta soni (himoya: katta custom oraliqda ham
#: javob hajmi cheklangan bo'lib qoladi — so'rov baza uchun og'ir bo'lmaydi).
MAX_SERIES_POINTS = 400


class AnalyticsError(ValueError):
    """Noto'g'ri oraliq/sana (HTTP 400 ga aylanadi)."""


# ---------------------------------------------------------------------------
# Vaqt yordamchilari
# ---------------------------------------------------------------------------
def _now() -> datetime:
    """Baza bilan bir xil soat: hisobot mintaqasi (Toshkent), naive.

    Server soati (Railway'da UTC) ishlatilmaydi — "bugun"/custom oraliq
    chegaralari va baza vaqtlari bir xil mintaqada bo'ladi.
    """
    return local_now()


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def parse_date(value: Any, *, end_of_day: bool = False) -> datetime:
    """``YYYY-MM-DD`` yoki ISO satrni ``datetime`` ga aylantiradi.

    Xato format / bo'sh qiymat -> :class:`AnalyticsError`.
    """
    text = str(value or "").strip()
    if not text:
        raise AnalyticsError("Sana ko'rsatilmagan (YYYY-MM-DD).")
    try:
        if len(text) == 10:  # faqat sana
            day = datetime.strptime(text, "%Y-%m-%d")
            if end_of_day:
                return day.replace(hour=23, minute=59, second=59)
            return day
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise AnalyticsError(
            f"Sana formati xato: {text} (YYYY-MM-DD bo'lishi kerak)."
        ) from exc


def resolve_range(
    name: str = DEFAULT_RANGE,
    start: Any = None,
    end: Any = None,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """So'ralgan oraliqni tekshiradi va ``{range, since, until, bucket}`` beradi.

    Tekshiruvlar (foydalanuvchi kiritishi mumkin bo'lgan qiymatlar):

    * noma'lum oraliq nomi -> xato;
    * ``custom`` uchun ikkala sana SHART;
    * ``start > end`` -> xato;
    * KELASI KUN -> xato ("bugun" ruxsat etiladi va hozirgi paytgacha
      qisqartiriladi);
    * oraliq ``MAX_RANGE_DAYS`` dan uzun -> xato;
    * natija ``MAX_SERIES_POINTS`` nuqtadan ko'p bo'lsa — kunlik bucket'ga
      o'tkaziladi (so'rov og'irlashmasligi uchun).
    """
    moment = now or _now()
    key = str(name or DEFAULT_RANGE).strip().lower()
    if key not in VALID_RANGES:
        raise AnalyticsError(
            "Noma'lum oraliq: "
            f"{name!r} (24h | 7d | 30d | custom bo'lishi kerak)."
        )

    if key == RANGE_CUSTOM:
        since = parse_date(start)
        until = parse_date(end, end_of_day=True)
        if since > until:
            raise AnalyticsError("Boshlanish sanasi tugash sanasidan keyin bo'lmasin.")
        # Faqat KELASI KUN rad etiladi: ``YYYY-MM-DD`` sanasi kun oxirigacha
        # (23:59:59) kengaytiriladi, shu sababli "bugun"ni tanlash ham
        # noto'g'ri "kelajak" deb hisoblanmasligi kerak — bugungi oyna hozirgi
        # paytgacha qisqartiriladi.
        if until.date() > moment.date():
            raise AnalyticsError("Kelajakdagi sana qabul qilinmaydi.")
        if until > moment:
            until = moment
        span = until - since
        if span.total_seconds() < 60:
            raise AnalyticsError("Oraliq juda qisqa (kamida 1 daqiqa).")
        if span.days > MAX_RANGE_DAYS:
            raise AnalyticsError(
                f"Oraliq juda uzun (maksimal {MAX_RANGE_DAYS} kun)."
            )
        # Kunlik oraliq — custom uchun standart; juda katta oraliqda ham
        # nuqtalar soni cheklangan qoladi.
        bucket = "hour" if span.total_seconds() <= 2 * 86400 else "day"
        return {"range": key, "since": since, "until": until, "bucket": bucket}

    preset = _PRESETS[key]
    until = moment
    since = moment - timedelta(days=preset["days"])
    return {
        "range": key,
        "since": since,
        "until": until,
        "bucket": preset["bucket"],
    }


# ---------------------------------------------------------------------------
# Ma'lumot yig'ish
# ---------------------------------------------------------------------------
async def overview(since: datetime, until: datetime) -> dict[str, int]:
    """Asosiy ko'rsatkichlar (bitta SQL so'rov + ban ro'yxati)."""
    from app.database import db
    from app.services import moderation

    data = await db.analytics_overview(_iso(since), _iso(until))
    data["users_banned"] = moderation.count_banned()
    return data


async def series(
    since: datetime, until: datetime, bucket: str = "day"
) -> list[dict]:
    """Vaqt kesimidagi seriya (bo'sh oraliqlar 0 bilan to'ldiriladi)."""
    from app.database import db

    rows = await db.analytics_series(_iso(since), _iso(until), bucket)
    return _fill(rows, since, until, bucket)


def _bucket_key(moment: datetime, bucket: str) -> str:
    return moment.strftime("%Y-%m-%dT%H" if bucket == "hour" else "%Y-%m-%d")


def _fill(
    rows: list[dict], since: datetime, until: datetime, bucket: str
) -> list[dict]:
    """Seriyani uzluksiz qiladi: yo'q bucket'lar 0 bilan to'ldiriladi."""
    known = {str(row.get("bucket") or ""): row for row in rows}
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    if bucket == "hour":
        cursor = since.replace(minute=0, second=0, microsecond=0)
    else:
        cursor = since.replace(hour=0, minute=0, second=0, microsecond=0)

    result: list[dict] = []
    guard = 0
    while cursor <= until and guard < MAX_SERIES_POINTS:
        guard += 1
        key = _bucket_key(cursor, bucket)
        row = known.get(key)
        result.append(
            row
            if row
            else {"bucket": key, "messages": 0, "deleted": 0, "edited": 0, "media": 0}
        )
        cursor += step
    # Ma'lumotda bo'lsa-yu, to'ldirishga tushmagan bucket (chegara aniqligi):
    if not result:
        result = sorted(rows, key=lambda row: str(row.get("bucket") or ""))
    return result


async def dashboard(
    range_name: str = DEFAULT_RANGE,
    start: Any = None,
    end: Any = None,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """To'liq analytics javobi (ko'rsatkichlar + seriya + meta).

    Xato holatda :class:`AnalyticsError` ko'tariladi (HTTP 400).
    """
    from app.utils.timeutils import REPORT_UTC_OFFSET_HOURS

    window = resolve_range(range_name, start, end, now=now)
    since, until = window["since"], window["until"]
    bucket = window["bucket"]
    stats = await overview(since, until)
    points = await series(since, until, bucket)
    return {
        "range": window["range"],
        "bucket": bucket,
        "since": _iso(since),
        "until": _iso(until),
        "timezone": {
            "offset_hours": REPORT_UTC_OFFSET_HOURS,
            "note": "Baza va hisob-kitob hisobot mintaqasi vaqtida (Toshkent, UTC+5).",
        },
        "metrics": stats,
        "series": points,
    }
