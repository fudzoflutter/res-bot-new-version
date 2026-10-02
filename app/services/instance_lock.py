"""
Bir vaqtda faqat BITTA nusxa polling qilishi uchun qulf (heartbeat).

NEGA KERAK
----------
Telegram bitta token uchun faqat bitta ``getUpdates`` ulanishiga ruxsat
beradi.  Ikki nusxa ishga tushsa (masalan, VS Code oynasidagi eski nusxa +
terminaldagi yangisi yoki serverga deploy qilingani), Telegram ularga
navbatma-navbat 409 Conflict qaytaradi va update'lar IKKI NUSXA orasida
bo'linib ketadi:

    "bot ba'zi xabarlarni ko'radi, ba'zilarini ko'rmaydi,
     ba'zi hisobotlar umuman kelmaydi"

Bu qulf shuni to'xtatadi: har bir nusxa ishga tushganda bazadagi
``instance_lock`` jadvalida "men tirikman" deb yozadi (heartbeat).  Boshqa
TIRIK nusxa bo'lsa — yangi nusxa polling boshlamaydi va sababini aniq
aytib to'xtaydi.

Muhim: qulf chegara (stale) bilan ishlaydi — nusxa qattiq o'ldirilgan
bo'lsa (taskkill), heartbeat ``STALE_SECONDS`` dan keyin eskiradi va
keyingi start qulfni o'zi oladi.  Shoshilinch holatda ``FORCE_POLL=1``
(env.txt) qulfni butunlay o'chirib qo'yadi.

DEPLOY (Railway/Render) HOLATI
------------------------------
Platformada har yangi deploy ESKI konteynerni darhol o'ldirmaydi: yangi
nusxa ishga tushgach, eski nusxa biroz vaqt (overlap) tirik turadi va
faqat keyin SIGTERM oladi.  Shu sababli yangi deploy ishga tushganda
qulfdagi heartbeat hali "yangi" (60 sekunddan yosh) bo'ladi va ilgarigi
mantiq yangi nusxani "ikkinchi nusxa" deb to'xtatib, BOTNI BUTUNLAY
O'CHIRIB QO'YARDI — qulfdagi eski nusxa esa tez orada o'ldirilardi.

Yechim: qulf yozuvi endi ``service`` va ``deployment`` identifikatorlarini
saqlaydi (Railway ``RAILWAY_SERVICE_ID`` / ``RAILWAY_DEPLOYMENT_ID``).
BOSHQARILADIGAN nusxa (``service`` berilgan) SHU servisning eski qulfini
majburan oladi — bu xavfsiz, chunki platforma eski nusxani albatta
almashtiradi (yangi deploy ham, qayta ishga tushirish ham shu holatga
to'g'ri keladi).  Eski nusxa keyingi heartbeat'ida qulfni yo'qotadi va
pollingni to'xtatadi (:func:`heartbeat_loop`), shuning uchun ikki nusxa
birga qolib update'larni bo'lishib yubora olmaydi.  BOSHQA servisning
qulfiga hech qachon tegilmaydi.

Bu qulf ESKI kod bilan ishlayotgan nusxani ko'ra olmaydi — bunday holatni
:mod:`app.services.duplicate_watch` aniqlaydi (409 Conflict kuzatuvchisi).
"""

from __future__ import annotations

from app.utils.timeutils import local_now

import asyncio
import logging
import os
import socket
from datetime import datetime
from typing import Awaitable, Callable, Optional

from app.config import settings
from app.database import db

logger = logging.getLogger(__name__)

# Har shuncha sekundda "men tirikman" deb yozamiz (1 ta yengil UPDATE).
HEARTBEAT_SECONDS = 20.0

# Heartbeat shu sekunddan eski bo'lsa — nusxa O'LGAN hisoblanadi va qulfni
# boshqa nusxa egallashi mumkin.  O'lchov: HEARTBEAT_SECONDS dan 3 barobar.
STALE_SECONDS = 60.0

# SHU KOMPYUTERDAGI eski nusxa qulfni bo'shatmay o'ldirilgan bo'lsa
# (taskkill //F), yangi start shuncha sekundgacha kutib turadi — nusxa
# o'lganiga ishonch hosil qilib, keyin qulfni o'zi oladi.  Shu tariqa
# "o'ldirdim-u qayta ishga tushirmoqchi edim, "ishlayapti" deb to'xtatdi"
# degan holat bo'lmaydi.
WAIT_FOR_LOCAL_SECONDS = 70.0
WAIT_STEP_SECONDS = 10.0
LOCK_DB_ATTEMPTS = 3
LOCK_DB_RETRY_SECONDS = 1.0

# Hozirgi nusxa identifikatori (ishga tushganda o'rnatiladi).
_instance: Optional[str] = None

# Platforma (deploy) identifikatorlari — qulfdagi yozuv bilan solishtiriladi.
_service: Optional[str] = None
_deployment: Optional[str] = None


def hostname() -> str:
    """Kompyuter/server nomi (log va xabar uchun; sir emas)."""
    try:
        return socket.gethostname() or "nomalum"
    except OSError:  # noqa: BLE001 – juda kam holat
        return "nomalum"


def instance_id() -> str:
    """Nusxani taniydigan nom: ``kompyuter:PID``."""
    return f"{hostname()}:{os.getpid()}"


def _env_first(*keys: str) -> Optional[str]:
    """Berilgan muhit o'zgaruvchilaridan birinchisining qiymati (bo'shmasi yo'q)."""
    for key in keys:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return None


def deployment_identity() -> tuple[Optional[str], Optional[str]]:
    """Joriy nusxaning ``(service, deployment)`` identifikatori.

    Platforma (Railway, Render, ...) bu qiymatlarni o'zi beradi:

    * Railway — ``RAILWAY_SERVICE_ID`` (barcha deploylar uchun bir xil) va
      ``RAILWAY_DEPLOYMENT_ID`` (har yangi deployda boshqacha).
    * Umumiy/DIY — ``DEPLOY_SERVICE`` va ``DEPLOY_ID`` (Docker/systemd
      foydalanuvchi qo'lda o'rnatishi mumkin).

    Mahalliy ishga tushirishda ikkisi ham ``None`` bo'ladi — u holda qulf
    avvalgidek qat'iy ishlaydi (bir xil kompyuterda ikkinchi nusxa
    to'xtatiladi).
    """
    return (
        _env_first("RAILWAY_SERVICE_ID", "RAILWAY_SERVICE_NAME", "DEPLOY_SERVICE"),
        _env_first("RAILWAY_DEPLOYMENT_ID", "DEPLOY_ID"),
    )


def current_instance() -> Optional[str]:
    """Shu jarayonning qulfdagi nomi (testlar uchun ham foydali)."""
    return _instance


def _age_seconds(stamp: Optional[str]) -> Optional[int]:
    """ISO vaqt yozuvidan necha sekund o'tganini hisoblaydi."""
    if not stamp:
        return None
    try:
        return max(0, int((local_now() - datetime.fromisoformat(stamp)).total_seconds()))
    except (TypeError, ValueError):
        return None


def duplicate_start_message(holder: dict) -> str:
    """Ikkinchi nusxa uchun aniq, bajariladigan ko'rsatma (log uchun)."""
    age = _age_seconds(holder.get("heartbeat_at"))
    if age is None:
        beat = "nomalum"
    else:
        beat = f"{age} sekund oldin"
    return (
        "BOT ALLAQACHON ISHLAYAPTI — ikkinchi nusxa ishga tushirilmadi.\n"
        "  Faol nusxa : {inst}\n"
        "  Kompyuter  : {host}\n"
        "  PID        : {pid}\n"
        "  Ishga tushgan: {started}\n"
        "  Oxirgi heartbeat: {beat}\n\n"
        "Telegram bitta token uchun faqat BITTA nusxaga xabar beradi, shuning\n"
        "uchun ikki nusxa birga ishlasa update'lar bo'linib ketadi (409\n"
        "Conflict) va ba'zi hisobotlar umuman kelmaydi.\n\n"
        "Nima qilish kerak:\n"
        "  1. Yuqoridagi nusxani to'xtating (o'sha terminalda Ctrl+C yoki\n"
        "     taskkill //PID {pid} //F), YOKI\n"
        "  2. shu nusxani ishga tushirishni xohlasangiz, env.txt ga\n"
        "     FORCE_POLL=1 yozib qayta ishga tushiring (mas'uliyat sizda:\n"
        "     409 xatolari davom etadi).\n"
        "  Nusxa qattiq o'ldirilgan bo'lsa, qulf {stale} sekunddan keyin\n"
        "  o'zi bo'shaydi — shuncha kutib qayta urinib ko'ring."
    ).format(
        inst=holder.get("instance") or "?",
        host=holder.get("host") or "?",
        pid=holder.get("pid") or "?",
        started=holder.get("started_at") or "?",
        beat=beat,
        stale=int(STALE_SECONDS),
    )


async def acquire() -> Optional[dict]:
    """Qulfni olishga urinadi.

    ``None``  — qulf bizda, polling boshlash mumkin.
    ``dict``  — boshqa TIRIK nusxa bor; qiymat o'sha nusxa yozuvi
                (chaqiruvchi polling boshlamasligi kerak).
    """
    global _instance, _service, _deployment
    _instance = instance_id()
    _service, _deployment = deployment_identity()

    if settings.force_poll:
        logger.warning(
            "FORCE_POLL=1 — bir nusxa qulfi o'chirilgan (%s). Ikki nusxa "
            "birga ishlasa 409 Conflict bo'ladi: xabarlar bo'linib ketadi.",
            _instance,
        )
        return None

    host = hostname()
    waited = 0.0
    while True:
        holder = None
        last_error: Exception | None = None
        for attempt in range(1, LOCK_DB_ATTEMPTS + 1):
            try:
                holder = await db.claim_instance_lock(
                    _instance, host, os.getpid(), STALE_SECONDS, _service, _deployment
                )
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning(
                    "Bir nusxa qulfini olishda DB xatosi (urinish %s/%s)",
                    attempt, LOCK_DB_ATTEMPTS, exc_info=True,
                )
                if attempt < LOCK_DB_ATTEMPTS:
                    await asyncio.sleep(LOCK_DB_RETRY_SECONDS * attempt)
        if last_error is not None:
            # Telegram long-polling uchun noma'lum lock holatida ishga tushish
            # xavfli: ikki nusxa update'larni bo'lib yuborishi mumkin.
            raise RuntimeError(
                "Instance lock holatini aniqlab bo'lmadi; polling fail-closed to'xtatildi"
            ) from last_error

        if holder is None:
            logger.info("Bir nusxa qulfi olindi: %s", _instance)
            return None

        # Egasi SHU kompyuterda bo'lsa, ehtimol uni hozir o'ldirdik (yoki u
        # o'layapti): heartbeat eskirishini kutamiz, keyin qulfni olamiz.
        # (Boshqariladigan deploy bu holatga yetib kelmaydi — u qulfni
        # yuqoridagi SQL orqali darhol oladi.)
        same_host = (holder.get("host") or "") == host
        if not same_host or waited >= WAIT_FOR_LOCAL_SECONDS:
            return holder
        logger.warning(
            "Boshqa nusxa (%s, PID %s) hali ham tirik ko'rinmoqda — %d sekund "
            "kutilyapti. Topilgan nusxa: logs/ ichidagi eski terminal oynasi?",
            holder.get("instance"),
            holder.get("pid"),
            int(WAIT_FOR_LOCAL_SECONDS - waited),
        )
        await asyncio.sleep(WAIT_STEP_SECONDS)
        waited += WAIT_STEP_SECONDS


async def heartbeat_loop(
    on_lost: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    """Har ``HEARTBEAT_SECONDS`` da qulf "tirik" ekanini tasdiqlaydi.

    Bitta yengil UPDATE — 20 sekundda bir marta, ya'ni tezlikka ta'siri
    o'lchab bo'lmaydigan darajada kichik.

    ``on_lost`` — qulf boshqa nusxaga (masalan YANGI deployga) o'tib
    ketganda chaqiriladi: shu jarayon pollingni to'xtatishi kerak, aks
    holda ikki nusxa birga update o'qib, 409 Conflict bo'ladi.
    """
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        if _instance is None:
            continue
        try:
            mine = await db.heartbeat_instance_lock(_instance)
        except Exception:  # noqa: BLE001 – baza uzilsa ham qayta urinamiz
            logger.exception("Heartbeat yozilmadi (bir nusxa qulfi)")
            continue
        if not mine:
            # Qulfni boshqa nusxa olib qo'ydi: ikki nusxa BIRGA poll
            # qilmasligi uchun shu jarayon to'xtatilishi kerak.
            logger.error(
                "Bir nusxa qulfini boshqa jarayon oldi (yangi deploy?) — "
                "bu nusxa pollingni to'xtatadi, aks holda update'lar ikki "
                "nusxa orasida bo'linib ketadi (409 Conflict)."
            )
            if on_lost is not None:
                try:
                    await on_lost()
                except Exception:  # noqa: BLE001 – to'xtatish urinishi yiqilmasin
                    logger.exception("Qulf yo'qolganda pollingni to'xtatib bo'lmadi")
            return


async def release() -> None:
    """Chiqishda qulfni bo'shatadi (keyingi start darhol ishga tushadi)."""
    if _instance is None:
        return
    try:
        await db.release_instance_lock(_instance)
        logger.info("Bir nusxa qulfi bo'shatildi: %s", _instance)
    except Exception:  # noqa: BLE001 – o'chish hech qachon yiqilmasin
        logger.info("Qulfni bo'shatib bo'lmadi (baza yopilgan bo'lishi mumkin)")
