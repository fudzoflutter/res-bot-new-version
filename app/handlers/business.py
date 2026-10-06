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

from app.emoji_config import EMOJI

import logging
import time
from pathlib import Path
from typing import Optional

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    BufferedInputFile,
    Message,
    ReplyParameters,
    User as TgUser,
)

from app.database import db
from app.services import admin_roles, alerts
from app.services import permissions as perms
from app.services.reporter import (
    Reporter,
    invalidate_connection,
    mark_connection_state,
)
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
                f"{EMOJI.maintenance_connection.tag} <b>Maintenance mode</b>\n\n"
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
    state_word = f"{EMOJI.connect_title.plain} ULANDI" if effective_enabled else f"{EMOJI.offline_dot.plain} UZILDI"
    action_word = "ulandi" if effective_enabled else "uzildi"
    detail_word = f"faollashtirildi {EMOJI.ok.plain}" if effective_enabled else f"o'chirildi {EMOJI.failed.plain}"
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
    """Qo'llab-quvvatlanadigan media turini va file_id ni qaytaradi."""
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.video_note:
        return "video_note", message.video_note.file_id
    return None


async def _save_replied_view_once(message: Message, bot: Bot) -> bool:
    """Ownerning ``?`` reply triggerini permanent nusxaga aylantiradi.

    Nusxa Business chatga emas, ulanish egasining private bot chatiga
    yuboriladi. Ikki OWNER/ADMIN orasidagi triggerda nusxa so‘ragan
    adminning private bot chatiga yuboriladi.  Yuborishda ataylab
    ``business_connection_id`` berilmaydi.

    ``True`` — bu xabar View Once trigger sifatida tanildi (muvaffaqiyatli
    saqlangan yoki saqlashga urinilgan); ``False`` — oddiy business_message.
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

    # Ordinary users can save only in their own connection.
    # Cross-connection saving is allowed only between OWNER/ADMIN accounts.
    started = time.monotonic()
    conn = await db.get_connection(connection_id)
    if not conn or not conn.get("is_enabled"):
        logger.warning("View Once: faol connection topilmadi (%s)", connection_id)
        return True

    owner_id = int(conn["user_id"])
    sender_id = message.from_user.id if message.from_user else None
    admin_roles_allowed = {admin_roles.ROLE_OWNER, admin_roles.ROLE_ADMIN}
    cross_admin = (
        sender_id is not None and sender_id != owner_id
        and admin_roles.role_of(sender_id) in admin_roles_allowed
        and admin_roles.role_of(owner_id) in admin_roles_allowed
    )
    if sender_id != owner_id and not cross_admin:
        logger.info(
            "View Once: begona ? trigger e'tiborsiz (conn=%s sender=%s owner=%s)",
            connection_id, sender_id, owner_id,
        )
        return True

    destination = sender_id if cross_admin else (conn.get("user_chat_id") or conn.get("user_id"))
    if not destination:
        logger.warning("View Once: owner private chat topilmadi (%s)", connection_id)
        return True
    destination = int(destination)

    # Poll/restart yoki Telegram retry bir xil triggerni ikki marta saqlamasin.
    dedupe_key = (
        f"view_once_saved:{connection_id}:{message.chat.id}:{message.message_id}"
    )
    if await db.get_setting(dedupe_key, "") == "1":
        logger.info("View Once: duplicate trigger tashlab yuborildi (%s)", dedupe_key)
        return True

    media_type, file_id = media

    emoji, media_name = {
        "photo": (EMOJI.view_once_photo, "Rasm"),
        "video": (EMOJI.view_once_video, "Video"),
        "video_note": (EMOJI.view_once_video_note, "Aylana video"),
    }[media_type]
    saved_text = f"{emoji.tag} <b>{media_name} saqlandi</b> · View Once"

    sent_as_video = False

    async def send_media(media_input):
        nonlocal sent_as_video
        # No business_connection_id: send only to the connection owner's bot chat.
        if media_type == "photo":
            return await tg_call("view_once_photo", lambda: bot.send_photo(
                destination, media_input, caption=saved_text, parse_mode="HTML",
            ))
        elif media_type == "video":
            return await tg_call("view_once_video", lambda: bot.send_video(
                destination, media_input, caption=saved_text, parse_mode="HTML",
                supports_streaming=True,
            ))
        else:
            try:
                return await tg_call("view_once_video_note", lambda: bot.send_video_note(
                    destination, media_input,
                ))
            except TelegramBadRequest as exc:
                if "VOICE_MESSAGES_FORBIDDEN" not in exc.message.upper():
                    raise
                # Recipient privacy may reject video notes but accept ordinary video.
                # Reuse the same bytes/reference; never turn media into a .bin document.
                result = await tg_call("view_once_video_fallback", lambda: bot.send_video(
                    destination, media_input, caption=saved_text,
                    parse_mode="HTML", supports_streaming=True,
                ))
                sent_as_video = True
                return result

    lookup_ms = int((time.monotonic() - started) * 1000)
    delivery_started = time.monotonic()
    delivery = "file_id"
    try:
        try:
            # Telegram reuses its stored file. No get_file/download/upload round trip.
            sent_media = await send_media(file_id)
        except TelegramBadRequest as exc:
            # Retry only a rejected media reference, never an ambiguous network send.
            reason = str(exc).lower()
            if not any(marker in reason for marker in (
                "wrong file identifier", "invalid file id", "file_id_invalid",
                "file_reference_expired", "file reference expired", "file not found",
                "can't use file of type selfdestructingphoto as photo",
                "can't use file of type selfdestructingvideo as video",
                "can't use file of type selfdestructingvideonote as videonote",
            )):
                raise
            logger.info("View Once file_id rejected: type=%s reason=%s", media_type, exc.message)
            delivery = "upload"
            tg_file = await tg_call("view_once_get_file", lambda: bot.get_file(file_id))
            if not tg_file.file_path:
                raise RuntimeError("Telegram file_path qaytarmadi")
            stream = await tg_call(
                "view_once_download", lambda: bot.download_file(tg_file.file_path),
            )
            raw = stream.read()
            if not raw:
                raise RuntimeError("Yuklangan media bo'sh")
            suffix = Path(tg_file.file_path).suffix
            if not suffix:
                suffix = ".jpg" if media_type == "photo" else ".mp4"
            sent_media = await send_media(BufferedInputFile(raw, filename=f"view_once{suffix}"))

        send_ms = int((time.monotonic() - delivery_started) * 1000)
        await db.set_setting(dedupe_key, "1")
        if media_type == "video_note" and not sent_as_video:
            # Video notes have no caption. Keep the receipt linked to the saved media.
            # Failure of the receipt must never resend the already delivered video.
            try:
                await tg_call("view_once_receipt", lambda: bot.send_message(
                    destination, saved_text, parse_mode="HTML",
                    reply_parameters=ReplyParameters(
                        message_id=sent_media.message_id, allow_sending_without_reply=True,
                    ),
                ))
            except Exception:
                logger.exception("View Once receipt failed: conn=%s", connection_id)
        logger.info(
            "View Once saved: conn=%s chat=%s trigger=%s target=%s type=%s delivery=%s shape=%s lookup_ms=%s send_ms=%s total_ms=%s",
            connection_id,
            message.chat.id,
            message.message_id,
            target.message_id,
            media_type,
            delivery,
            "video" if sent_as_video else media_type,
            lookup_ms,
            send_ms,
            int((time.monotonic() - started) * 1000),
        )
    except Exception:  # noqa: BLE001 – monitoring bot ishlashda davom etsin
        logger.exception(
            "View Once saqlanmadi: conn=%s chat=%s trigger=%s target=%s type=%s",
            connection_id,
            message.chat.id,
            message.message_id,
            target.message_id,
            media_type,
        )

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
    # View Once triggerni reporter keshidan oldin ishlaymiz.  Reporter baribir
    # pastda chaqiriladi — mavjud delete/edit monitoring logikasi buzilmaydi.
    await _save_replied_view_once(message, bot)

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
