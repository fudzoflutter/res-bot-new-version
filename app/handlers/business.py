"""
Business-ulanish handlerlari.

Bot biror foydalanuvchining "Telegram Business → Chatbotlar"iga qo'shilganda:

* ``business_connection``          – ulanish o'rnatildi / uzildi
* ``business_message``             – chatlarida xabar yuborildi
* ``edited_business_message``      – xabar tahrirlandi
* ``deleted_business_messages``    – xabar(lar) o'chirildi

Hisobot QOIDALARI:

* yuborilgan xabarlar FORVARD qilinmaydi — tarkib jim KESHlanadi;
* matn xabari           -> faqat TAHRIRLANGANDA yoki O'CHIRILGANDA xabar;
* media (rasm/video/GIF/stiker/ovozli xabar/dumaloq video/musiqa/fayl) ->
  faqat O'CHIRILGANDA xabar (fayl qayta yuboriladi);
* hisobot faqat SUHBATDOSH hodisalari uchun — eganing o'z yuborgan/
  tahrirlagan/o'chirgan xabarlari hech qachon hisobot qilib berilmaydi.

MAXFIYLIK: Connection A ning ma'lumotlari Connection B egasiga kelmaydi.
Har bir hisobot FAQAT shu ulanish egasiga (user_chat_id) boriladi.

MAINTENANCE MODE: yoqilganda business monitoring TO'XTAMAYDI — incoming
xabarlar kesh/DBga saqlanadi, edit/delete holatlari ham yangilanadi. Faqat
foydalanuvchiga yuboriladigan edit/delete hisobotlari vaqtincha jim qilinadi;
shu sababli Hold Mode paytida monitoring ma'lumoti yo'qolmaydi.

Har bir hodisa DBga (statistika + activity log) va adminga xabar qilinadi.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Optional

from aiogram import Bot, Router
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    FSInputFile,
    Message,
    User as TgUser,
)

from app.database import db
from app.services import admin_roles, alerts, subscriptions
from app.services import permissions as perms
from app.services.reporter import (
    Reporter,
    invalidate_connection,
    mark_connection_state,
)
from app.keyboards import user_kb
from app.utils import texts
from app.utils.telegram_api import call as tg_call

router = Router(name="business")
logger = logging.getLogger(__name__)


def _mention(user: TgUser) -> str:
    """Kichik mahalliy eslatma yordamchisi."""
    from app.utils.formatting import mention_by_id

    name = user.first_name or user.username or str(user.id)
    return mention_by_id(user.id, name, user.username)


async def _maintenance_on() -> bool:
    """Hold (maintenance) rejimi yoqilganmi?

    Holat :mod:`app.services.maintenance` da (bazada) saqlanadi — admin panel
    va Web Admin AYNAN shu bayroqni o'zgartiradi.
    """
    from app.services import maintenance

    return await maintenance.is_enabled()


# ---------------------------------------------------------------------------
# Ulanish o'rnatildi / uzildi
# ---------------------------------------------------------------------------
@router.business_connection()
async def on_connection(connection: BusinessConnection, bot: Bot) -> None:
    """Ulanish, uzilish yoki huquqlar o'zgarganda ishga tushadi.

    MAXFIYLIK: har bir hisobot FAQAT shu ulanish egasiga boriladi.
    """
    is_enabled = bool(connection.is_enabled)
    user = connection.user

    # Takroriy connection update (Telegram qayta yuborishi mumkin) — ikki
    # marta qayta ishlanmasin.  MUHIM: yutilish FAQAT kiruvchi holat shu
    # ulanish uchun oxirgi ishlangan holat bilan BIR XIL bo'lganda amalga
    # oshadi.  Holat O'ZGARGAN bo'lsa (enabled -> disabled -> enabled) u
    # DARHOL ishlanadi — aks holda reconnect TTL ichida yo'qolib qolardi.
    if mark_connection_state(connection.id, is_enabled):
        logger.info(
            "Takroriy business_connection tashlab yuborildi: %s (enabled=%s)",
            connection.id, is_enabled,
        )
        return

    # Foydalanuvchini ro'yxatga olish (/start bosmagan bo'lishi mumkin).
    await db.upsert_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )
    existed = await db.get_connection(connection.id) is not None    # ---------------------------------------------------------------- ACCESS.
    # Ban / Ruxsat tizimi va Maintenance mode:

    maintenance_block = bool(is_enabled) and not existed and await _maintenance_on()

    # Maintenance paytida yangi connection'ni HAQIQIY Telegram holatidan ajratmaymiz.
    # Connection DB'da real holatda qoladi. Business message/edit/delete monitoring
    # ham davom etadi; faqat user-facing hisobotlar Hold tugaguncha yuborilmaydi.
    # Shu bilan data yo'qolmaydi va stale "disabled" holat paydo bo'lmaydi.
    effective_enabled = is_enabled

    if maintenance_block:
        logger.info(
            "Maintenance mode: yangi connection saqlandi, monitoring davom etadi; user hisobotlari vaqtincha to'xtatilgan (%s, owner=%s)",
            connection.id, user.id,
        )

    await db.upsert_connection(
        business_connection_id=connection.id,
        user_id=user.id,
        is_enabled=effective_enabled,
        user_chat_id=connection.user_chat_id,
    )
    # Ulanish holati o'zgardi — in-memory keshni yangilaymiz.
    invalidate_connection(connection.id)

    # 1) Ulanish egasiga xabar (faqat uning o'ziga).
    notify_chat = connection.user_chat_id or user.id
    try:
        if maintenance_block:
            text = (
                "🛠 <b>Maintenance mode</b>\n\n"
                "Bot hozir texnik xizmat rejimida. Yangi ulanish saqlandi. "
                "Monitoring davom etadi, lekin foydalanuvchiga hisobotlar vaqtincha yuborilmaydi."
            )
        elif effective_enabled:
            text = (
                texts.BUSINESS_ENABLED_AGAIN if existed
                else texts.BUSINESS_CONNECTED
            )
        else:
            text = texts.BUSINESS_DISABLED
        await tg_call(
            "notify_connection",
            lambda: bot.send_message(notify_chat, text, parse_mode="HTML"),
            attempts=2,
        )
    except Exception:  # noqa: BLE001 – foydalanuvchi botni bloklagan bo'lishi mumkin
        logger.info("Could not notify user %s about connection change", user.id)

    # 2) Statistika yozuvi.
    state_word = "🔗 ULANDI" if effective_enabled else "🔴 UZILDI"
    action_word = "ulandi" if effective_enabled else "uzildi"
    detail_word = "faollashtirildi ✅" if effective_enabled else "o'chirildi ❌"
    await db.add_event(
        user_id=user.id,
        event_type="connection",
        details=f"{action_word} ({connection.id})",
        business_connection_id=connection.id,
    )

    # 3) Activity log — talab qilingan hodisalar.
    if maintenance_block:
        activity_event, activity_text, severity = (
            "connection_maintenance",
            f"Maintenance mode: connection saqlandi, monitoring davom etadi; user hisobotlari to'xtatildi ({connection.id})",
            "WARNING",
        )
    elif effective_enabled:
        activity_event, activity_text, severity = (
            "connection_created" if not existed else "connection_enabled",
            f"Biznes-ulanish {'yaratildi' if not existed else 'faollashtirildi'} "
            f"({connection.id})",
            "INFO",
        )
    else:
        activity_event, activity_text, severity = (
            "connection_removed" if not existed else "connection_disabled",
            f"Biznes-ulanish {'uzildi' if existed else 'rad etildi'} ({connection.id})",
            "WARNING",
        )
    try:
        await db.add_activity_log(
            activity_event,
            activity_text,
            connection_id=connection.id,
            user_id=user.id,
            severity=severity,
        )
    except Exception:  # noqa: BLE001
        logger.debug("connection activity log yozilmadi", exc_info=True)

    # 4) Adminlarga real-time xabar (ko'p adminli arxitektura: hammasiga).
    notice = (
        f"{state_word} — {_mention(user)} biznes-ulanishi {detail_word}.\n"
        f"<code>{connection.id}</code>"
    )
    notification_admins = [
        user_id
        for user_id, role in admin_roles.list_admins().items()
        if perms.role_has(role, perms.P_CONNECTIONS_VIEW)
    ]
    for admin_chat in sorted(notification_admins):
        try:
            await tg_call(
                "notify_admin_connection",
                lambda chat=admin_chat: bot.send_message(
                    chat, notice, parse_mode="HTML"
                ),
                attempts=2,
            )
        except Exception:  # noqa: BLE001 – bittasi yuborilmasa qolgani davom
            logger.info("Ulanish xabari adminga (%s) yuborilmadi", admin_chat)

    # 5) MUHIM hodisa: ulanish uzildi — adminga ALERT (cooldown bilan).
    if existed and not effective_enabled:
        username = f"@{user.username}" if user.username else (user.first_name or user.id)
        await alerts.alerts.notify(
            f"connection_disabled:{connection.id}",
            "BUSINESS CONNECTION",
            f"Ulanish uzildi.\nOwner: {username}\n"
            f"Connection ID: {connection.id}",
            severity=alerts.SEVERITY_WARNING,
            also_log=False,
            connection_id=connection.id,
            user_id=user.id,
        )

    logger.info(
        "Business connection %s: user=%s enabled=%s (effective=%s)",
        connection.id, user.id, is_enabled, effective_enabled,
    )


# ---------------------------------------------------------------------------
# View Once saqlash: owner media ustiga aynan "?" bilan reply qilsa
# ---------------------------------------------------------------------------

def _view_once_media(message: Message) -> Optional[tuple[str, str]]:
    """Qo'llab-quvvatlanadigan View Once media turi va file_id ni qaytaradi."""
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.video_note:
        return "video_note", message.video_note.file_id
    return None


def _view_once_name(media_type: str) -> str:
    if media_type == "photo":
        return "view_once.jpg"
    if media_type == "video_note":
        return "view_once_video_note.mp4"
    return "view_once_video.mp4"


async def _send_view_once_direct(
    bot: Bot, destination: int, media_type: str, file_id: str
) -> None:
    """Telegram serveridagi mavjud ``file_id`` ni qayta ishlatib yuboradi.

    Bu eng tez yo'l: Railway media baytlarini yuklab olmaydi.
    Self-destructing photo bu yo'lni Telegram tomonidan rad etadi, shuning
    uchun photo uchun chaqiruvchi darhol fallbackga o'tadi.
    """
    if media_type == "video":
        await tg_call(
            "view_once_send_video_direct",
            lambda: bot.send_video(
                destination,
                video=file_id,
                supports_streaming=True,
            ),
            attempts=2,
            timeout=60.0,
        )
        return

    if media_type == "video_note":
        await tg_call(
            "view_once_send_video_note_direct",
            lambda: bot.send_video_note(destination, video_note=file_id),
            attempts=2,
            timeout=60.0,
        )
        return

    # View Once rasm Bot API'da ``SelfDestructingPhoto`` sifatida keladi.
    # Uni send_photo(file_id) bilan qayta ishlatish taqiqlangan; befoyda
    # 400 so'rov yubormaymiz, darhol download/upload fallback ishlaydi.
    raise RuntimeError("self-destructing photo requires upload fallback")


async def _download_view_once_temp(
    bot: Bot, file_id: str, media_type: str
) -> tuple[str, int]:
    """View Once faylni RAMga to'liq olmay, temp faylga oqim bilan yuklaydi."""
    suffix = ".jpg" if media_type == "photo" else ".mp4"
    fd, path = tempfile.mkstemp(prefix="view_once_", suffix=suffix)
    os.close(fd)
    try:
        with open(path, "wb") as fp:
            await tg_call(
                "view_once_download",
                lambda: bot.download(file_id, destination=fp),
                attempts=2,
                timeout=180.0,
            )
        size = os.path.getsize(path)
        if size <= 0:
            raise RuntimeError("View Once fayl bo'sh yuklandi")
        return path, size
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


async def _send_view_once_upload(
    bot: Bot, destination: int, media_type: str, path: str
) -> str:
    """Temp faylni owner chatiga yuboradi.

    Asl media turi Telegram cheklovi sabab qabul qilinmasa, kontent baribir
    yo'qolmasligi uchun oddiy video/document ko'rinishiga tushadi.
    Qaytaradi: amalda ishlatilgan yuborish turi.
    """
    filename = _view_once_name(media_type)

    if media_type == "photo":
        try:
            await tg_call(
                "view_once_send_photo_upload",
                lambda: bot.send_photo(
                    destination, photo=FSInputFile(path, filename=filename)
                ),
                attempts=2,
                timeout=180.0,
            )
            return "photo"
        except Exception:
            logger.info(
                "View Once photo asl turida yuborilmadi; document fallback",
                exc_info=True,
            )
            await tg_call(
                "view_once_send_photo_document",
                lambda: bot.send_document(
                    destination, document=FSInputFile(path, filename=filename)
                ),
                attempts=2,
                timeout=180.0,
            )
            return "document"

    if media_type == "video":
        try:
            await tg_call(
                "view_once_send_video_upload",
                lambda: bot.send_video(
                    destination,
                    video=FSInputFile(path, filename=filename),
                    supports_streaming=True,
                ),
                attempts=2,
                timeout=240.0,
            )
            return "video"
        except Exception:
            logger.info(
                "View Once video asl turida yuborilmadi; document fallback",
                exc_info=True,
            )
            await tg_call(
                "view_once_send_video_document",
                lambda: bot.send_document(
                    destination, document=FSInputFile(path, filename=filename)
                ),
                attempts=2,
                timeout=240.0,
            )
            return "document"

    # video_note: avval aylana video sifatida saqlashga harakat qilamiz.
    # Bot API upload qilingan video_note uchun 1 daqiqalik limit qo'yadi;
    # juda uzun/nomuvofiq fayl bo'lsa oddiy video, keyin document yuboramiz.
    try:
        await tg_call(
            "view_once_send_video_note_upload",
            lambda: bot.send_video_note(
                destination,
                video_note=FSInputFile(path, filename=filename),
            ),
            attempts=2,
            timeout=240.0,
        )
        return "video_note"
    except Exception:
        logger.info(
            "View Once video_note aylana ko'rinishida yuborilmadi; video fallback",
            exc_info=True,
        )

    try:
        await tg_call(
            "view_once_send_video_note_as_video",
            lambda: bot.send_video(
                destination,
                video=FSInputFile(path, filename=filename),
                supports_streaming=True,
            ),
            attempts=2,
            timeout=240.0,
        )
        return "video"
    except Exception:
        logger.info(
            "View Once video_note video sifatida yuborilmadi; document fallback",
            exc_info=True,
        )

    await tg_call(
        "view_once_send_video_note_document",
        lambda: bot.send_document(
            destination, document=FSInputFile(path, filename=filename)
        ),
        attempts=2,
        timeout=240.0,
    )
    return "document"


async def _save_replied_view_once(message: Message, bot: Bot) -> bool:
    """Owner ``?`` bilan reply qilgan View Once mediani private botga saqlaydi.

    Strategiya:
      * video/video_note: avval eng tez ``file_id`` direct yuborish;
      * photo: SelfDestructingPhoto direct taqiqlangani uchun darhol fallback;
      * direct rad etilsa: RAMga yig'masdan temp faylga download -> upload;
      * video_note upload cheklansa: oddiy video -> document fallback.

    ``True`` — ``?`` View Once trigger sifatida tanildi; ``False`` — oddiy
    business_message.
    """
    if (message.text or "").strip() != "?" or not message.reply_to_message:
        return False

    target = message.reply_to_message
    media = _view_once_media(target)
    if media is None:
        return False

    connection_id = message.business_connection_id
    if not connection_id:
        return False

    # Xavfsizlik: trigger faqat business connection egasining o'z xabari.
    conn = await db.get_connection(connection_id)
    if not conn or not conn.get("is_enabled"):
        logger.warning("View Once: faol connection topilmadi (%s)", connection_id)
        return True

    owner_id = int(conn["user_id"])
    sender_id = message.from_user.id if message.from_user else None
    if sender_id != owner_id:
        logger.info(
            "View Once: begona ? trigger e'tiborsiz (conn=%s sender=%s owner=%s)",
            connection_id, sender_id, owner_id,
        )
        return True

    destination = conn.get("user_chat_id") or conn.get("user_id")
    if not destination:
        logger.warning("View Once: owner private chat topilmadi (%s)", connection_id)
        return True
    destination = int(destination)

    # Premium gate: obuna rejimi O'CHIQ bo'lsa View Once avvalgidek bepul.
    # Rejim YOQILGANDA avval obuna tekshiriladi, shundan KEYINGINA file_id
    # download/direct send boshlanadi — obunasiz user media baytini olmaymiz.
    sub_cfg = await subscriptions.get_config()
    if sub_cfg.enabled and not await subscriptions.is_active(owner_id):
        try:
            await tg_call(
                "view_once_premium_required",
                lambda: bot.send_message(
                    destination,
                    "🔒 <b>View Once — Premium funksiya</b>\n\n"
                    "Rasm, video va aylana videolarni saqlash uchun Premium obuna kerak.\n\n"
                    f"💎 {sub_cfg.days} kun — {sub_cfg.price:,} so'm".replace(",", " "),
                    parse_mode="HTML",
                    reply_markup=user_kb.premium_required_menu(),
                ),
                attempts=2,
            )
        except Exception:  # noqa: BLE001
            logger.info("View Once premium xabari yuborilmadi (owner=%s)", owner_id)
        logger.info(
            "View Once Premium gate: obunasiz owner (conn=%s owner=%s)",
            connection_id, owner_id,
        )
        return True

    dedupe_key = (
        f"view_once_saved:{connection_id}:{message.chat.id}:{message.message_id}"
    )
    if await db.get_setting(dedupe_key, "") == "1":
        logger.info("View Once: duplicate trigger tashlab yuborildi (%s)", dedupe_key)
        return True

    media_type, file_id = media
    direct_error: Optional[BaseException] = None

    # 1) DIRECT — faqat bu media turi amalda qayta ishlatilishi mumkin bo'lsa.
    if media_type != "photo":
        try:
            await _send_view_once_direct(bot, destination, media_type, file_id)
            await db.set_setting(dedupe_key, "1")
            logger.info(
                "View Once DIRECT: conn=%s chat=%s trigger=%s target=%s type=%s",
                connection_id,
                message.chat.id,
                message.message_id,
                target.message_id,
                media_type,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            direct_error = exc
            logger.warning(
                "View Once DIRECT rad etildi; STREAM fallback: "
                "conn=%s chat=%s trigger=%s target=%s type=%s error=%s",
                connection_id,
                message.chat.id,
                message.message_id,
                target.message_id,
                media_type,
                str(exc)[:220],
            )

    # 2) STREAM FALLBACK — SelfDestructingPhoto / FILE_REFERENCE_EXPIRED va
    # shu kabi holatlar. Fayl to'liq RAMga olinmaydi.
    temp_path: Optional[str] = None
    try:
        temp_path, size = await _download_view_once_temp(bot, file_id, media_type)
        sent_as = await _send_view_once_upload(
            bot, destination, media_type, temp_path
        )
        await db.set_setting(dedupe_key, "1")
        logger.info(
            "View Once STREAM: conn=%s chat=%s trigger=%s target=%s "
            "type=%s sent_as=%s bytes=%s direct_error=%s",
            connection_id,
            message.chat.id,
            message.message_id,
            target.message_id,
            media_type,
            sent_as,
            size,
            type(direct_error).__name__ if direct_error else "skipped",
        )
    except Exception:  # noqa: BLE001 – monitoring bot ishlashda davom etsin
        logger.exception(
            "View Once STREAM yuborilmadi: conn=%s chat=%s trigger=%s "
            "target=%s type=%s",
            connection_id,
            message.chat.id,
            message.message_id,
            target.message_id,
            media_type,
        )
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                logger.debug("View Once temp fayl o'chmadi: %s", temp_path)

    return True


# ---------------------------------------------------------------------------
# Xabar yuborildi (stiker / rasm / video ham shu yerda)
# ---------------------------------------------------------------------------
@router.business_message()
async def on_business_message(message: Message, bot: Bot) -> None:
    """Biznes-ulanish orqali kelgan har qanday xabar — FAQAT keshlanadi.

    Talab: yuborilgan xabarlar (matn, stiker, rasm, video, GIF,
    ovozli xabar, dumaloq video, musiqa, fayl) egaga FORVARD qilinmaydi.
    Tarkib jim saqlanadi — keyin o'chirilsa, aynan nima o'chirilgani
    ko'rsatilishi uchun.
    """
    # View Once triggerni reporter keshidan oldin ishlaymiz. Agar owner media
    # ustiga aynan "?" bilan reply qilgan bo'lsa, file_id orqali darhol
    # private bot chatiga yuboramiz va shu control xabarni boshqa pipeline ga
    # kiritmaymiz. Bu View Once javobini maksimal tezlashtiradi.
    if await _save_replied_view_once(message, bot):
        return

    if await _maintenance_on():
        logger.info(
            "Maintenance mode: business_message monitoring davom etadi (conn=%s mid=%s)",
            message.business_connection_id, message.message_id,
        )
    logger.info(
        "business_message: conn=%s chat=%s mid=%s",
        message.business_connection_id,
        message.chat.id if message.chat else None,
        message.message_id,
    )
    await _safe_report(Reporter(bot).report_incoming(message))


# ---------------------------------------------------------------------------
# Xabar tahrirlandi
# ---------------------------------------------------------------------------
@router.edited_business_message()
async def on_edited(message: Message, bot: Bot) -> None:
    """Tahrirlangan xabar -> hisobot faqat SUHBATDOSH matn tahriri uchun.

    Egasining o'z tahriri va media (izoh) tahriri jim keshlanadi.
    """
    hold = await _maintenance_on()
    if hold:
        logger.info(
            "Maintenance mode: edit saqlanadi, hisobot yuborilmaydi (conn=%s mid=%s)",
            message.business_connection_id, message.message_id,
        )
    logger.info(
        "edited_business_message: conn=%s chat=%s mid=%s",
        message.business_connection_id,
        message.chat.id if message.chat else None,
        message.message_id,
    )
    await _safe_report(Reporter(bot).report_edited(message, notify=not hold))


# ---------------------------------------------------------------------------
# Xabar(lar) o'chirildi
# ---------------------------------------------------------------------------
@router.deleted_business_messages()
async def on_deleted(deleted: BusinessMessagesDeleted, bot: Bot) -> None:
    """O'chirilgan xabarlar -> hisobot faqat SUHBATDOSH xabarlari uchun.

    Matn bo'lsa ASL MATN chiqariladi, media bo'lsa keshlangan fayl QAYTA
    YUBORILADI.  Egasining o'z xabarlari va keshda yo'q idlar jim o'tadi.

    MAXFIYLIK: hisobot FAQAT shu ulanish egasiga (owner) boriladi.
    """
    hold = await _maintenance_on()
    if hold:
        logger.info(
            "Maintenance mode: delete saqlanadi, hisobot yuborilmaydi (conn=%s ids=%s)",
            deleted.business_connection_id, deleted.message_ids,
        )
    logger.info(
        "deleted_business_messages: conn=%s chat=%s ids=%s",
        deleted.business_connection_id,
        deleted.chat.id if deleted.chat else None,
        deleted.message_ids,
    )
    await _safe_report(Reporter(bot).report_deleted(deleted, notify=not hold))


async def _safe_report(coro) -> None:  # type: ignore[type-arg]
    """Hisobot xatosi botni ishdan chiqarmasligi kerak."""
    try:
        await coro
    except Exception:  # noqa: BLE001
        logger.exception("Reporter xatosi (hisobot yuborilmadi)")


def connection_owner(connection_id: Optional[str]) -> Optional[int]:
    """Kichik yordamchi: ulanish egasining chat IDsi (testlar uchun)."""
    from app.services.reporter import _connection_cache

    if not connection_id:
        return None
    cached = _connection_cache.get(connection_id)
    return cached[2] if cached else None
