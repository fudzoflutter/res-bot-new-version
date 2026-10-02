"""
Umumiy vaqt yordamchilari (DB qatlamlari + hisobotlar uchun).

Ikkala backend (SQLite va Supabase Postgres) bir xil vaqt formatini
ishlatadi: ISO-8601 TEXT, HISOBOT MINTAQASI (Toshkent, UTC+5) vaqti, tzinfo'siz
(:func:`local_now`).  Server soatiga (UTC/local) BOG'LIQ EMAS.

HISOBOT VAQTI (muhim)
---------------------
Hisobotlarda ko'rinadigan vaqt SERVER soatiga bog'liq bo'lmasligi kerak:
Railway/Docker konteynerida soat UTC (ya'ni Toshkentdan 5 soat ORQADA),
VPS yoki boshqa mintaqada esa boshqacha.  Shu sababli hisobot vaqti doim
:data:`TZ_REPORT` mintaqasida ko'rsatiladi (Toshkent, UTC+5) — bot qayerda
ishga tushishidan qat'i nazar bir xil va TO'G'RI.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

# "Onlayn" hisobi uchun oyna (sekund) — count_online bilan bir xil qiymat.
ONLINE_WINDOW_SECONDS = 120

# ---------------------------------------------------------------------------
# HISOBOT SOAT MINTAQASI
#
# O'zgartirish kerak bo'lsa — faqat SHU QATOR: masalan 6 (UTC+6),
# 3 (UTC+3, Moskva), 0 (UTC).
# Toshkentda yozgi vaqt (DST) yo'q, shuning uchun qat'iy siljish yetarli.
# ---------------------------------------------------------------------------
REPORT_UTC_OFFSET_HOURS = 5

TZ_REPORT = timezone(timedelta(hours=REPORT_UTC_OFFSET_HOURS))
UTC_ZONE = timezone.utc


def now_report() -> datetime:
    """Hisobot vaqti mintaqasidagi ``hozir`` (offset-aware).

    Mashinaning o'z soat mintaqasiga BOG'LIQ EMAS: avval UTC olinadi
    (:func:`datetime.now` bilan tz beriladi), keyin hisobot mintaqasiga
    o'giriladi.
    """
    return datetime.now(UTC_ZONE).astimezone(TZ_REPORT)


def to_report(value: Optional[datetime]) -> Optional[datetime]:
    """Berilgan vaqtni hisobot mintaqasiga o'giradi.

    Naive (tzinfo'siz) qiymat UTC deb olinadi — chunki serverlar odatda
    UTCda yuradi va naive vaqtni mahalliy deb hisoblash xatolarni
    keltirib chiqaradi.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC_ZONE)
    return value.astimezone(TZ_REPORT)


def hms(value: Optional[datetime] = None) -> str:
    """``HH:MM:SS`` — hisobot mintaqasida (Toshkent, UTC+5).

    ``None`` berilsa — hozirgi vaqt.  Hisobotlardagi BARCHA vaqtlar shu
    funksiyadan o'tadi, shuning uchun bitta joyni o'zgartirish yetarli.
    """
    moment = to_report(value) if value is not None else now_report()
    return (moment or now_report()).strftime("%H:%M:%S")


def local_now() -> datetime:
    """DB va analytics uchun YAGONA "hozir": hisobot mintaqasi (Toshkent) vaqti, NAIVE.

    Nega ``datetime.now()`` emas: Railway/Docker'da server soati UTC, VPS'da
    boshqa mintaqa bo'lishi mumkin.  Bazadagi barcha vaqtlar, analytics
    chegaralari ("bugun", custom oraliq kunlari) va hisobotlar SHU soatda
    bo'ladi — server qayerda ishlashidan qat'i nazar bir xil.

    Qaytadigan qiymat tzinfo'siz (ISO satr sifatida saqlash/taqqoslash uchun).
    """
    return now_report().replace(tzinfo=None)


def now_iso() -> str:
    """Hozirgi hisobot-mintaqa vaqti ISO satr sifatida (formatning yagona joyi)."""
    return local_now().isoformat(timespec="seconds")


def iso_before(seconds: int) -> str:
    """Shu soniya oldingi vaqt (ISO) — DBdagi 'online' chegarasi bilan bir xil."""
    return (local_now() - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def is_online(
    last_activity: Optional[str], window_seconds: int = ONLINE_WINDOW_SECONDS
) -> bool:
    """``last_activity`` shu oyna ichidami?

    DB qatlamlari bilan BIR XIL mantiq: ISO-8601 satrlar leksikografik
    taqqoslanadi (format bir xil bo'lgani uchun to'g'ri ishlaydi).  Shu
    tufayli "online" sonini qo'shimcha DB so'rovisiz hisoblash mumkin.
    """
    return bool(last_activity) and str(last_activity) >= iso_before(window_seconds)


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp stored in the DB (None-safe)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
