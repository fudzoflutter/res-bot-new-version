"""
Offline tests for the "single instance" guard and the 409 conflict watcher.

Run from the project root:

    python tests/duplicate_test.py

MUAMMO (nega bu testlar bor)
----------------------------
Telegram bitta token uchun faqat BITTA nusxaga update beradi. Ikki nusxa
ishlasa ular navbatma-navbat 409 Conflict oladi va update'lar IKKIGA
bo'linib ketadi — "bot ba'zi xabarlarni ko'radi, ba'zilarini ko'rmaydi".
Aiogram 409 xatosini O'ZI yutib qo'yadi, shuning uchun bu holat jimgina
davom etadi. Bundan tashqari, eski kod bilan ishlayotgan nusxa yangi
qo'shilgan turlarni (ovozli xabar, dumaloq video) umuman hisoblamaydi.

Testlar:
PART A — instance_lock: qulf ATOMIK olinadi, ikkinchi nusxa to'xtatiladi,
         heartbeat ishlaydi, o'lgan nusxaning qulfi (stale) bo'shatiladi,
         chiqishda qulf bo'shaydi.
PART A2 — shu kompyuterdagi o'lik nusxa kutishni talab qilmaydi.
PART A3 — Railway (deploy): boshqariladigan nusxa SHU servisning eski
         qulfini (yangi deploy / qayta ishga tushirish / replika) oladi;
         boshqa servisning qulfiga tegilmaydi.
PART B — FORCE_POLL=1 qulfni o'chiradi; xabar matni egasining ma'lumotini
         ko'rsatadi.
PART C — ConflictWatcher: 60 sekundda 3 ta 409 -> bitta ogohlantirish;
         qayta ogohlantirish faqat cooldown (30 daqiqa) dan keyin; 409
         bo'lmagan yozuvlar sanalmaydi; alert xato bersa ham logging
         yiqilmaydi.
PART D — handler haqiqiy aiogram logger'iga ulanadi va uziladi.
"""

from __future__ import annotations

import sys as _sys

# Windows konsolida emoji chop etish uchun (cp1252 UnicodeEncodeError bermasin).
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import logging
import os
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN"
os.environ["ADMIN_ID"] = "111111111"
os.environ["CODEBUFF_SKIP_ENV_FILE"] = "1"
os.environ.pop("SUPABASE_DB_URL", None)

_tmpdir = tempfile.mkdtemp(prefix="bot_duplicate_test_")
os.environ["DB_PATH"] = str(Path(_tmpdir) / "test.db")

from app.config import settings  # noqa: E402
from app.database import db  # noqa: E402
from app.services import duplicate_watch, instance_lock  # noqa: E402
from app.utils.tasks import drain  # noqa: E402

WATCHED = duplicate_watch.WATCHED_LOGGER
CONFLICT_TEXT = (
    "Failed to fetch updates - TelegramConflictError: Telegram server says - "
    "Conflict: terminated by other getUpdates request; make sure that only "
    "one bot instance is running"
)


def _log_record(message: str, name: str = WATCHED) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


# ---------------------------------------------------------------------------
# PART A / B — qulf
# ---------------------------------------------------------------------------
async def part_a_lock() -> None:
    await db.init()

    # 1) Bo'sh baza: qulf A ga o'tadi.
    assert await db.claim_instance_lock("A", "pc-1", 100, 60) is None, "A qulfni olishi kerak"

    # 2) Ayni shu nusxa qayta so'rasa — o'zi to'sib qo'ymaydi.
    assert await db.claim_instance_lock("A", "pc-1", 100, 60) is None

    # 3) Boshqa TIRIK nusxa: qulf berilmaydi va egasi qaytadi.
    holder = await db.claim_instance_lock("B", "pc-2", 200, 60)
    assert holder is not None, "B ga qulf berilmasligi kerak"
    assert holder["instance"] == "A", holder
    assert holder["host"] == "pc-1" and int(holder["pid"]) == 100, holder

    # 4) Heartbeat: faqat qulf egasi yangilay oladi.
    assert await db.heartbeat_instance_lock("A") is True
    assert await db.heartbeat_instance_lock("B") is False, "begona heartbeat o'tmasin"

    # 5) O'lgan nusxa (stale): heartbeat juda eski -> B qulfni oladi.
    assert await db.claim_instance_lock("B", "pc-2", 200, 0) is None, "stale qulf olinishi kerak"
    assert await db.heartbeat_instance_lock("A") is False, "A endi qulf egasi emas"
    assert await db.heartbeat_instance_lock("B") is True

    # 6) Chiqishda bo'shatish: keyingi nusxa darhol ishga tushadi.
    await db.release_instance_lock("B")
    assert await db.claim_instance_lock("C", "pc-3", 300, 60) is None
    # Begona nusxa boshqaning qulfini bo'shata olmaydi.
    await db.release_instance_lock("A")
    assert await db.heartbeat_instance_lock("C") is True, "C ning qulfi turishi kerak"
    await db.release_instance_lock("C")

    # 7) Servis qatlami (app/services/instance_lock.py) shu bazadan foydalanadi.
    assert await instance_lock.acquire() is None, "bo'sh qulf servis orqali olinishi kerak"
    mine = instance_lock.current_instance()
    assert mine and ":" in mine, mine
    assert await db.heartbeat_instance_lock(mine) is True

    holder = await db.claim_instance_lock("boshqa-nusxa", "pc-9", 900, 60)
    assert holder is not None and holder["instance"] == mine, holder

    # 8) Xabar matni: kim ishlayotganini aniq aytadi.
    text = instance_lock.duplicate_start_message(holder)
    assert mine in text and "pc-9" not in text
    assert "409" in text and "FORCE_POLL" in text and "Ctrl+C" in text, text

    # 9) FORCE_POLL=1 — qulf o'chadi (foydalanuvchi ongli ravishda majburlaydi).
    await instance_lock.release()
    object.__setattr__(settings, "force_poll", True)
    try:
        assert await instance_lock.acquire() is None, "FORCE_POLL da qulf tekshirilmaydi"
    finally:
        object.__setattr__(settings, "force_poll", False)

    print("PART A/B (bir nusxa qulfi) PASSED ✅")


# ---------------------------------------------------------------------------
# PART A2 — shu kompyuterdagi o'lik nusxa kutishni talab qilmaydi
# ---------------------------------------------------------------------------
async def part_a2_wait() -> None:
    # 1) TIRIK nusxa (heartbeat yangi): kutish tugagach start to'xtatiladi.
    await db.claim_instance_lock("eski-nusxa", socket.gethostname(), 4242, 60)
    old_wait, old_step = (
        instance_lock.WAIT_FOR_LOCAL_SECONDS,
        instance_lock.WAIT_STEP_SECONDS,
    )
    instance_lock.WAIT_FOR_LOCAL_SECONDS = 0.05
    instance_lock.WAIT_STEP_SECONDS = 0.02
    try:
        holder = await instance_lock.acquire()
        assert holder is not None, "tirik nusxa bilan start to'xtatilishi kerak"
        assert holder["instance"] == "eski-nusxa", holder
        assert "eski-nusxa" in instance_lock.duplicate_start_message(holder)
    finally:
        instance_lock.WAIT_FOR_LOCAL_SECONDS = old_wait
        instance_lock.WAIT_STEP_SECONDS = old_step

    # 2) O'LGAN nusxa (heartbeat eskirgan — taskkill //F): kutmasdan olinadi.
    old_stale = instance_lock.STALE_SECONDS
    instance_lock.STALE_SECONDS = 0.0
    try:
        assert await instance_lock.acquire() is None, (
            "eskirgan qulf darhol olinishi kerak (o'lik nusxa)"
        )
        mine = instance_lock.current_instance()
        assert await db.heartbeat_instance_lock(mine) is True
    finally:
        instance_lock.STALE_SECONDS = old_stale
    await instance_lock.release()

    print("PART A2 (o'lik nusxa kutishni talab qilmaydi) PASSED ✅")


# ---------------------------------------------------------------------------
# PART A3 — yangi deploy eski deploy qulfini egallaydi (Railway)
# ---------------------------------------------------------------------------
async def part_a3_new_deploy_takes_over() -> None:
    """Railway: boshqariladigan nusxa SHU servisning eski (tirik) qulfini oladi.

    Platforma yangi versiyani chiqarganda (yoki konteynerni qayta ishga
    tushirganda) eski nusxa bir necha sekund tirik turadi va uning
    heartbeat'i hali "yangi" bo'ladi.  Ilgarigi mantiq yangi nusxani
    "ikkinchi nusxa" deb to'xtatib, botni butunlay o'chirib qo'yardi.
    Endi ``service`` bir xil bo'lsa, qulf boshqariladigan nusxaga o'tadi.
    """
    os.environ["RAILWAY_SERVICE_ID"] = "svc-1"
    os.environ["RAILWAY_DEPLOYMENT_ID"] = "dep-old"
    try:
        # Eski nusxa qulfni ushlab turadi (heartbeat YANGI — hali o'lmagan).
        await db.claim_instance_lock(
            "eski-nusxa", "old-host", 1, 60, "svc-1", "dep-old"
        )

        # Yangi deploy (boshqa deployment id): qulfni oladi.
        os.environ["RAILWAY_DEPLOYMENT_ID"] = "dep-new"
        assert await instance_lock.acquire() is None, "yangi deploy qulfni olishi kerak"
        mine = instance_lock.current_instance()
        assert mine and await db.heartbeat_instance_lock(mine) is True
        assert await db.heartbeat_instance_lock("eski-nusxa") is False, (
            "eski nusxa endi qulf egasi emas"
        )

        # Qayta ishga tushirish / replika (BIR XIL deployment id): heartbeat
        # yangi bo'lsa ham qulfni oladi — eski nusxa keyingi heartbeat'ida
        # to'xtaydi.
        holder = await db.claim_instance_lock(
            "replika-2", "host-2", 2, 60, "svc-1", "dep-new"
        )
        assert holder is None, "shu servisning keyingi nusxasi qulfni olishi kerak"
        assert await db.heartbeat_instance_lock(mine) is False, (
            "eski replika endi qulf egasi emas"
        )

        # BOSHQA servis qulfni tortib olmaydi.
        holder = await db.claim_instance_lock(
            "boshqa-bot", "host-3", 3, 60, "svc-2", "dep-x"
        )
        assert holder is not None, "boshqa servis qulfni olmasligi kerak"
        assert holder["instance"] == "replika-2", holder

        # Keyingi testlar toza holatdan boshlansin.
        await db.release_instance_lock("replika-2")
        await instance_lock.release()
    finally:
        os.environ.pop("RAILWAY_SERVICE_ID", None)
        os.environ.pop("RAILWAY_DEPLOYMENT_ID", None)

    print("PART A3 (boshqariladigan deploy qulfni oladi) PASSED ✅")


# ---------------------------------------------------------------------------
# PART C — 409 Conflict kuzatuvchisi
# ---------------------------------------------------------------------------
async def part_c_watcher() -> None:
    alerts: list[int] = []

    async def alert(total: int) -> None:
        alerts.append(total)

    watcher = duplicate_watch.ConflictWatcher(alert, cooldown_seconds=600.0)

    # 409 bo'lmagan yozuvlar sanalmaydi.
    watcher.emit(_log_record("Failed to fetch updates - TelegramNetworkError"))
    watcher.emit(_log_record(CONFLICT_TEXT, name="boshqa.logger"))
    await drain()
    assert alerts == [], "faqat 409 xatolari sanalishi kerak"
    assert watcher.total == 0

    # Chegaragacha (3) ogohlantirish YO'Q.
    watcher.emit(_log_record(CONFLICT_TEXT))
    watcher.emit(_log_record(CONFLICT_TEXT))
    await drain()
    assert alerts == [], "3 tadan oldin xabar berilmasin"
    assert watcher.total == 2

    # Uchinchisida — BITTA ogohlantirish.
    watcher.emit(_log_record(CONFLICT_TEXT))
    await drain()
    assert alerts == [3], alerts
    assert watcher.alerts == 1

    # Cooldown ichida yana 409 bersa — takroriy shovqin yo'q.
    for _ in range(5):
        watcher.emit(_log_record(CONFLICT_TEXT))
    await drain()
    assert alerts == [3], alerts
    assert watcher.total == 8, watcher.total

    # Cooldown o'tgach yana ogohlantiriladi (jami sanasi bilan).
    watcher._last_alert -= duplicate_watch.ALERT_COOLDOWN_SECONDS + 1
    watcher.emit(_log_record(CONFLICT_TEXT))
    await drain()
    assert alerts == [3, 9], alerts

    # Deraza (window) ishlaydi: sekin kelgan 409 lar "musobaqa" hisoblanmaydi.
    slow = duplicate_watch.ConflictWatcher(alert, window_seconds=0.01, cooldown_seconds=0.0)
    for _ in range(4):
        slow.emit(_log_record(CONFLICT_TEXT))
        await asyncio.sleep(0.02)
    await drain()
    assert slow.total == 4 and slow.alerts == 0, (slow.total, slow.alerts)

    # Alert xato bersa ham logging YIQILMAYDI (bot ishlashda davom etadi).
    async def broken(_total: int) -> None:
        raise RuntimeError("telegram yo'q")

    bad = duplicate_watch.ConflictWatcher(broken, cooldown_seconds=0.0)
    for _ in range(3):
        bad.emit(_log_record(CONFLICT_TEXT))   # xato ko'tarilmasligi kerak
    await drain()
    assert bad.alerts == 1

    print("PART C (409 kuzatuvchisi) PASSED ✅")


# ---------------------------------------------------------------------------
# PART D — handler haqiqiy logger'ga ulanadi
# ---------------------------------------------------------------------------
async def part_d_install() -> None:
    logger = logging.getLogger(WATCHED)
    before = list(logger.handlers)

    alerts: list[int] = []

    async def alert(total: int) -> None:
        alerts.append(total)

    watcher = duplicate_watch.install(alert)
    assert watcher in logger.handlers, "handler logger'ga ulanmadi"
    assert duplicate_watch.current() is watcher

    # Haqiqiy logger orqali yozilgan 409 xato ham sanaladi.
    for _ in range(3):
        logger.error("Failed to fetch updates - %s", "TelegramConflictError: Conflict: terminated by other getUpdates request")
    await drain()
    assert alerts == [3], alerts

    duplicate_watch.uninstall()
    assert watcher not in logger.handlers, "handler UZILMADI"
    assert logger.handlers == before
    assert duplicate_watch.current() is None

    print("PART D (handler ulanadi/uziladi) PASSED ✅")


async def run_all() -> None:
    try:
        await part_a_lock()
        await part_a2_wait()
        await part_a3_new_deploy_takes_over()
        await part_c_watcher()
        await part_d_install()
    finally:
        await drain()
        if instance_lock.current_instance():
            await instance_lock.release()
        await db.close()
    print("DUPLICATE / SINGLE-INSTANCE TESTS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(run_all())
