"""
Application entrypoint / wiring.

Reads top-to-bottom to understand how the bot is assembled:

1. config + logging + database (+ SQLite fallback) + alert sender
2. middlewares (metrics + registration)
3. routers (business, admin -> Web Mini App tugmasi, then user)
4. long polling + background watchdog + instance lock
"""

from __future__ import annotations

from app.emoji_config import EMOJI

import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import MenuButtonWebApp, TelegramObject, WebAppInfo

from app.config import ensure_configured, settings
from app.database import db
from app.handlers import admin, business, user
from app.middlewares import RegisterUserMiddleware
from app.keyboards.user_kb import webapp_url
from app.services import (
    admin_roles,
    alerts,
    connection_verify,
    instance_lock,
    maintenance,
    metrics,
    moderation,
)
from app.services.duplicate_watch import install as watch_duplicate_polling
from app.services.watchdog import notify_crash, run_watchdog
from app.utils import texts
from app.utils.logger import setup_logging
from app.utils.tasks import drain

logger = logging.getLogger(__name__)


class MetricsMiddleware(BaseMiddleware):
    """Health statistikasi: update soni, xatolar, ogohlantirishlar.

    Xatolar bu yerda YUTILMAYDI — faqat sanaladi va yuqoriga ko'tariladi.
    """

    async def __call__(self, handler, event: TelegramObject, data: dict):  # type: ignore[type-arg]
        metrics.record_update()
        try:
            return await handler(event, data)
        except Exception:
            metrics.record_error()
            raise


def _friendly_setup_errors(func):
    """Translate raw driver errors into clear setup instructions."""
    import asyncpg
    from aiogram.exceptions import (
        TelegramConflictError,
        TelegramNetworkError,
        TelegramUnauthorizedError,
    )

    async def wrapper():
        try:
            await func()
        except TelegramConflictError:
            logger.error(
                "BOT ALLAQACHON ISHLAB TURIBDI (409 Conflict).\n"
                "Boshqa terminal/VS Code oynasida run.py ochiq bo'lishi mumkin.\n"
                "O'sha oynani yoping (yoki Ctrl+C) va faqat BITTA nusxani ishga tushiring."
            )
            raise
        except TelegramUnauthorizedError:
            logger.error(
                "BOT_TOKEN rejected by Telegram (401 Unauthorized).\n"
                "Check .env: the token must be the CURRENT one from @BotFather "
                "(if you ever revoked it, the old one stops working)."
            )
            raise
        except TelegramNetworkError:
            logger.error(
                "Internet/Telegram aloqasi yo'q (network error).\n"
                "Ulanishni tekshirib, qaytadan ishga tushiring."
            )
            raise
        except (RuntimeError, asyncpg.InvalidPasswordError, OSError) as exc:
            logger.error("Setup problem: %s", exc)
            raise

    return wrapper


async def _notify_duplicate_poller(bot: Bot, total: int) -> None:
    """409 Conflict — botni boshqa nusxa ham poll qilyapti (jimgina qolmasin)."""
    metrics.record_warning()
    logger.error(
        "409 CONFLICT: botni boshqa nusxa ham poll qilmoqda (jami %s ta) — "
        "update'lar ikki nusxa orasida bo'linib ketmoqda!",
        total,
    )
    try:
        await bot.send_message(
            settings.admin_id,
            texts.DUPLICATE_POLLER.format(count=total),
            parse_mode="HTML",
        )
    except Exception:  # noqa: BLE001 – ogohlantirish yuborilmasa ham davom
        logger.info("409 ogohlantirishini yuborib bo'lmadi (admin chat?)")
    try:
        await db.add_activity_log(
            "conflict_409",
            f"409 Conflict: {total} marta (ikki nusxa polling qilmoqda)",
            severity="ERROR",
        )
    except Exception:  # noqa: BLE001
        pass


async def _watchdog_supervisor() -> None:
    """run_watchdog ni doim yashab turadi (o'lsa 30 s dan keyin tiklaydi).

    Qayta ishga tushish JIMGINA bo'lmaydi: admin alert + activity log
    (:func:`app.services.watchdog.notify_crash`).
    """
    while True:
        try:
            await run_watchdog()
        except asyncio.CancelledError:
            raise  # o'chirish signali — qayta boshlamaymiz
        except Exception as exc:  # noqa: BLE001 – watchdog hech qachon o'lib qolmasin
            logger.exception("Watchdog task crashed - restarting in 30s")
            try:
                await notify_crash(exc)
            except Exception:  # noqa: BLE001 – alert xatosi ham to'xtatmasin
                logger.debug("Watchdog crash alert yuborilmadi", exc_info=True)
            await asyncio.sleep(30)


async def _configure_admin_menu_button(bot: Bot) -> None:
    """Adminlar chatidagi «Menu» tugmasini Web Mini App'ga ulaydi.

    Admin botni ochib pastdagi Menu tugmasini bossa, panel to'g'ridan-to'g'ri
    Web Mini App'da ochiladi.  Xato bo'lsa (admin botni hali ochmagan) —
    jimgina o'tkazib yuboriladi.
    """
    ids = {int(settings.admin_id)}
    try:
        ids.update(int(uid) for uid in admin_roles.list_admins())
    except Exception:  # noqa: BLE001
        logger.debug("Admin ro'yxatini olib bo'lmadi", exc_info=True)
    button = MenuButtonWebApp(text="Admin panel", web_app=WebAppInfo(url=webapp_url()))
    for uid in sorted(ids):
        try:
            await bot.set_chat_menu_button(chat_id=uid, menu_button=button)
        except Exception as exc:  # noqa: BLE001
            logger.info("Menu tugmasi o'rnatilmadi (user=%s): %s", uid, exc)


async def _startup_warnings(bot: Bot) -> None:
    """Ishga tushishdagi xavfli holatlar haqida adminga aniq ogohlantiradi."""
    if settings.force_poll:
        logger.warning(
            "FORCE_POLL=1 — bir nusxa qulfi O'CHIRILGAN. Ikki nusxa birga "
            "ishlasa update'lar bo'linib ketadi (409 Conflict)."
        )
        await alerts.alerts.notify(
            "force_poll_enabled",
            "FORCE_POLL=1 YOQILGAN",
            "Bir nusxa qulfi o'chirilgan. Bu faqat diagnostika/ favqulodda "
            "holat uchun.\n"
            + (f"{EMOJI.warning.plain} Bu PRODUCTION muhit!\n" if settings.production else "")
            + "Productionda FORCE_POLL ni o'chiring (env: FORCE_POLL=0).",
            severity=(
                alerts.SEVERITY_CRITICAL
                if settings.production
                else alerts.SEVERITY_WARNING
            ),
            force=True,
        )
    if db.startup_error:
        await alerts.alerts.notify(
            "db_startup_fallback",
            "DATABASE ERROR",
            "Supabase ishga tushmadi — bot SQLite fallbackda ishlayapti.\n"
            f"Sabab: {db.startup_error}\n"
            f"Fallback: {db.backend_name}\n"
            "Retry: 3/3\n"
            "Supabase tiklanishi bilan QAYTA ISHGA TUSHIRING.",
            severity=alerts.SEVERITY_CRITICAL,
            force=True,
        )
        try:
            await db.add_activity_log(
                "db_fallback",
                f"Supabase ishga tushmadi — SQLite fallback "
                f"({db.startup_error[:120]})",
                severity="CRITICAL",
            )
        except Exception:  # noqa: BLE001
            pass


@_friendly_setup_errors
async def main() -> None:
    """Build and run the bot; runs until cancelled.

    ISHGA TUSHISH TARTIBI (deterministik — Railway rollback uchun muhim):

    1. logging + konfiguratsiya (xato bo'lsa darhol to'xtaydi)
    2. Bot + web server/healthcheck (port ochiladi, /health hali 503)
    3. baza initializatsiyasi (idempotent DDL, hech narsa o'chirilmaydi)
    4. Telethon/aiogram servislari: rollar, ulanishlar, hold mode keshi
    5. bir nusxa qulfi
    6. Dispatcher/handlerlar + fon vazifalari
    7. READY belgisi + polling

    BIROR QADAM XATO BERSA: xato logga yoziladi, jarayon 0 bo'lmagan kod
    bilan chiqadi va "/health" hech qachon 200 demaydi — Railway yangi
    deployni sog'lom deb hisoblamaydi (eski nusxa ishlashda qoladi).
    """
    setup_logging()
    logger.info(
        "STARTUP: ilova ishga tushmoqda (environment=%s, production=%s)",
        settings.environment,
        settings.production,
    )
    ensure_configured()
    logger.info("STARTUP: konfiguratsiya yuklandi (admin=%s)", settings.admin_id)

    bot = Bot(
        token=settings.bot_token,
        # HTML everywhere by default – handlers can simply include tags.
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Alertlar (DB xatosi, ulanish uzilishi, watchdog) — OWNERlarga.
    alerts.configure_default_sender(bot)
    # Hold mode + barcha foydalanuvchiga texnik xizmat xabarlari.
    maintenance.configure_notifier(bot)
    moderation.configure_notifier(bot)
    connection_verify.configure_bot(bot)

    # --- WEB SERVER / HEALTHCHECK -------------------------------------------
    # Port ATAYLAB erta ochiladi (bazadan oldin): Railway konteynerga darhol
    # ulanadi, /health esa ilova TAYYOR bo'lmaguncha 503 qaytaradi — shu
    # tufayli muvaffaqiyatsiz startup "sog'lom" deb ko'rsatilmaydi.
    from app.web import (
        mark_not_ready,
        mark_ready,
        recover_broadcast_state,
        start_web_server,
    )

    web_site = await start_web_server(bot)
    logger.info("STARTUP: web server/healthcheck tinglayapti")

    watchdog_task = None
    lock_task = None
    lock_acquired = False
    try:
        await db.init()
        logger.info("STARTUP: database initialized (backend=%s)", db.backend_name)

        # Broadcast holatini bazadan tiklaymiz — restart/deploy paytida
        # uzilgan broadcast abadiy "running" bo'lib qolmasin (aks holda yangi
        # broadcast doim 409 olardi) va oxirgi hisoblar yo'qolmasin.
        await recover_broadcast_state()

        # Rollar (owner/admin/moderator/viewer) va ban ro'yxati —
        # sozlamalardan (restart/deploy dan omon qoladi).
        await admin_roles.load()
        await moderation.load()

        # Hold mode — BAZADAGI holat (restart/deploy dan omon qoladi).
        logger.info(
            "STARTUP: hold mode = %s",
            "ON" if await maintenance.is_enabled(force=True) else "OFF",
        )

        # --- BIR NUSXA QULFI --------------------------------------------------
        # Telegram bitta tokenga faqat BITTA getUpdates beradi: ikki nusxa birga
        # ishlasa update'lar bo'linib ketadi (ba'zi hisobotlar kelmaydi).
        # Shuning uchun ikkinchi nusxa polling boshlamaydi va sababini aytadi.
        holder = await instance_lock.acquire()
        if holder is not None:
            message = instance_lock.duplicate_start_message(holder)
            logger.error(message)
            # Admin panelni ochmasa ham bilsin: qisqa ogohlantirish yuboriladi.
            try:
                await alerts.alerts.notify(
                    "instance_lock_held",
                    "IKKINCHI NUSXA ISHGA TUSHMADI",
                    message,
                    severity=alerts.SEVERITY_ERROR,
                    force=True,
                )
            except Exception:  # noqa: BLE001
                pass
            # finally bo'limi vebserverni to'xtatadi va bazani yopadi.
            # READY BERILMAYDI. Exception run_cli() orqali non-zero exit beradi,
            # shunda Railway/Docker bu nusxani muvaffaqiyatli deb belgilamaydi.
            raise RuntimeError("Instance lock band: boshqa tirik nusxa polling qilmoqda")
        lock_acquired = True
        try:
            await db.add_activity_log(
                "instance_lock_acquired",
                f"Bir nusxa qulfi olindi ({instance_lock.current_instance()})",
                severity="INFO",
            )
        except Exception:  # noqa: BLE001
            pass

        dp = Dispatcher(storage=MemoryStorage())

        # --- middlewares (run for every update) -----------------------------
        # Metrics: health ekrani uchun update/xato hisoblagichlari.
        dp.update.outer_middleware(MetricsMiddleware())
        # RegisterUser: foydalanuvchini DBga yozadi + faollikni yangilaydi
        # (HOLD rejimida oddiy foydalanuvchini ham to'xtatadi).
        dp.update.outer_middleware(RegisterUserMiddleware())

        # --- routers ---------------------------------------------------------
        # 1) business_* updates: connection notices + activity reports.
        dp.include_router(business.router)

        # 2) /admin — Web Mini App (TMA) tugmasi (FAQAT admin).
        dp.include_router(admin.router)

        # 3) regular users (menyu, statistika, havola tozalash) go last.
        dp.include_router(user.router)

        logger.info("STARTUP: Telegram servislari/handlerlar tayyor")

        # --- background jobs --------------------------------------------------
        watchdog_task = asyncio.create_task(_watchdog_supervisor())

        # Qulf boshqa nusxaga (masalan YANGI deployga) o'tsa, eski nusxa
        # pollingni to'xtatishi SHART — aks holda ikki nusxa birga update
        # o'qib, 409 Conflict boshlanadi va xabarlar bo'linib ketadi.
        async def _on_lock_lost() -> None:
            try:
                alert = alerts.SEVERITY_ERROR
                await alerts.alerts.notify(
                    "instance_lock_lost",
                    "INSTANCE LOCK YO'QOLDI",
                    "Qulfni boshqa nusxa oldi — bu nusxa pollingni to'xtatadi.",
                    severity=alert,
                    also_log=True,
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                await dp.stop_polling()
            except RuntimeError:
                pass  # polling hali boshlanmagan bo'lsa — to'xtatadigan narsa yo'q

        # Bir nusxa qulfining heartbeat'i: 20 sekundda bitta yengil UPDATE.
        lock_task = asyncio.create_task(
            instance_lock.heartbeat_loop(on_lost=_on_lock_lost)
        )

        # 409 (boshqa nusxa polling qilmoqda) bo'lsa adminga ANIQ xabar yuboriladi
        watch_duplicate_polling(
            lambda total: _notify_duplicate_poller(bot, total)
        )

        await _startup_warnings(bot)
        await _configure_admin_menu_button(bot)
        # drop_pending_updates=False: bot o'chiq turgan paytda to'plangan
        # update'lar (/start, business_connection, edited/deleted) YO'QOLMASIN —
        # ular start_polling orqali handlerlarga yetkaziladi.
        await bot.delete_webhook(drop_pending_updates=False)

        # --- READY ------------------------------------------------------------
        # Faqat shu yerga yetib kelgach /health 200 qaytaradi.  Undan oldin
        # 503 bo'ladi — ya'ni muvaffaqiyatsiz startup hech qachon "sog'lom"
        # ko'rinmaydi (Railway eski deployni saqlab qoladi).
        mark_ready()
        logger.info("Bot is starting (admin=%s)...", settings.admin_id)
        await dp.start_polling(bot)
    finally:
        # Avval healthcheck'ni yopamiz: to'xtayotgan nusxa sog'lom emas.
        mark_not_ready()
        if watchdog_task is not None:
            watchdog_task.cancel()
        if lock_task is not None:
            lock_task.cancel()
        await web_site.stop()
        # Fonda ketayotgan yozuvlar (masalan ro'yxatga olish) tugasin —
        # aks holda baza yopilgach ular xato beradi.
        await drain()
        if lock_acquired:
            # Qulfni bo'shatamiz: keyingi start darhol ishga tushadi (aks holda
            # qulf STALE_SECONDS gacha "band" bo'lib turadi).
            try:
                await db.add_activity_log(
                    "instance_lock_released",
                    f"Bir nusxa qulfi bo'shatildi ({instance_lock.current_instance()})",
                    severity="INFO",
                )
            except Exception:  # noqa: BLE001
                pass
            await instance_lock.release()
        await db.close()
        await bot.session.close()
        logger.info("Bot stopped.")


def run_cli() -> int:
    """Bloklovchi ishga tushirish (``python run.py`` / Docker CMD).

    Qaytaradi: ``0`` — toza to'xtash, ``1`` — startup/ish xatosi.
    NOL BO'LMAGAN kod MUHIM: aks holda Railway/Docker nosog'lom
    konteynerni "muvaffaqiyatli yakunlandi" deb hisoblaydi.
    """
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("STARTUP: ilova qo'lda to'xtatildi")
        return 0
    except Exception as exc:  # noqa: BLE001 – startup yoki ish vaqtidagi xato
        logger.error("STARTUP FAILED — ilova to'xtadi: %s", exc, exc_info=True)
        try:
            from app.web import mark_not_ready

            mark_not_ready()
        except Exception:  # noqa: BLE001 – healthcheck belgisi xato bersa ham chiqamiz
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
