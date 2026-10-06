"""
Foydalanuvchi tomonidagi handlerlar.

* /start              – asosiy menyu
* /admin              – (admin.py) Web Mini App'da admin panelni ochish
* Statistika          – shaxsiy statistika
* Ulanish             – tg://settings/edit orqali sozlamalarga yo'naltirish

Foydalanuvchilar boshqaruvi OLIB TASHLANGAN.
Monitoring ma'lumotlari (ulanishlar, xabarlar, o'chirilgan, tahrirlangan,
kesh, baza) faqat admin panelda ko'rinadi.

XAVFSIZLIK: oddiy foydalanuvchilar hech qanday monitoring ma'lumotiga
kira olmaydi — bu handlers darajasida tekshiriladi.
"""

from __future__ import annotations

from app.emoji_config import EMOJI

import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from app.database import db
from app.handlers.admin import ADMIN_PANEL_TEXT
from app.keyboards import user_kb
from app.services import admin_roles
from app.utils import texts
from app.utils.formatting import fmt_number, mention_by_id

router = Router(name="user")
logger = logging.getLogger(__name__)

# ESLATMA (tezlik): har bir callback handler AVVAL ``cb.answer()`` qiladi.


# ---------------------------------------------------------------------------
# /start – asosiy menyu
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Kirish nuqtasi: to'g'ridan-to'g'ri menyu."""
    await state.clear()

    try:
        is_connected = await db.has_active_connection(message.from_user.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cmd_start DB xatosi: %s", exc)
        is_connected = None

    is_admin = message.from_user and admin_roles.is_admin(message.from_user.id)
    markup = user_kb.main_menu(connected=bool(is_connected))
    if is_admin:
        markup.inline_keyboard.extend(user_kb.admin_panel_menu().inline_keyboard)
    await message.answer(
        texts.start_message(is_connected), reply_markup=markup,
        parse_mode="HTML", disable_web_page_preview=True,
    )

@router.callback_query(F.data == user_kb.CB_BACK_MENU)
async def back_to_menu(cb: CallbackQuery, state: FSMContext) -> None:
    """Har qanday ekrandan menyuga qaytish (ulanish holati bilan)."""
    await cb.answer()
    await state.clear()

    if admin_roles.is_admin(cb.from_user.id):
        await cb.message.edit_text(
            ADMIN_PANEL_TEXT, reply_markup=user_kb.admin_panel_menu()
        )
        return

    connected = await db.has_active_connection(cb.from_user.id)
    await cb.message.edit_text(
        texts.start_message(connected),
        reply_markup=user_kb.main_menu(connected=connected),
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Statistika
# ---------------------------------------------------------------------------
@router.callback_query(F.data == user_kb.CB_STATS)
async def show_stats(cb: CallbackQuery) -> None:
    """Shaxsiy statistika ekrani (faqat shu foydalanuvchining o'z ma'lumotlari).

    XAVFSIZLIK: oddiy foydalanuvchilar umumiy bot statistikasini ko'ra olmaydi.
    """
    await cb.answer()

    # Admin uchun statistika Web Mini App'da.
    if admin_roles.is_admin(cb.from_user.id):
        await cb.message.edit_text(
            ADMIN_PANEL_TEXT, reply_markup=user_kb.admin_panel_menu()
        )
        return

    try:
        stats = await db.user_stats(cb.from_user.id)
    except Exception:  # noqa: BLE001
        logger.exception("Statistika so'rovi bajarilmadi (user=%s)", cb.from_user.id)
        await cb.message.edit_text(
            texts.ERROR_USER, reply_markup=user_kb.back_to_menu()
        )
        return

    # Show only this user's own stats (not global stats)
    mention = mention_by_id(
        cb.from_user.id, cb.from_user.first_name or "User", cb.from_user.username
    )
    body = (
        f"{EMOJI.report_user.tag} Profil: {mention}\n"
        f"{EMOJI.report_id.tag} ID: <code>{cb.from_user.id}</code>\n\n"
        f"{EMOJI.connect_title.tag} Ulanish: {texts.CONNECTED_LINE if stats['active_connections'] else texts.NOT_CONNECTED_LINE}\n\n"
        f"{EMOJI.inbox.tag} Yozib olingan xabarlar: <b>{fmt_number(stats['events_total'])}</b>\n"
        f"{EMOJI.report_edit.tag} Tahrirlar: <b>{fmt_number(stats['edits'])}</b>\n"
        f"{EMOJI.report_delete.tag} O'chirishlar: <b>{fmt_number(stats['deletes'] + stats['deletes_media'])}</b>"
    )
    await cb.message.edit_text(
        f"{EMOJI.stats_header.tag} <b>Sizning statistikangiz</b>\n\n{body}",
        reply_markup=user_kb.back_to_menu(),
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Ulanish yo'riqnomasi
# ---------------------------------------------------------------------------
@router.callback_query(F.data == user_kb.CB_CONNECT)
async def show_connect(cb: CallbackQuery) -> None:
    """Sozlamalar → Telegram Business → Chatbotlar yo'riqnomasi."""
    await cb.answer()
    global _bot_username
    if not _bot_username:
        me = await cb.bot.me()
        _bot_username = me.username or ""
    await cb.message.edit_text(
        texts.CONNECT_TITLE.format(bot_username=_bot_username),
        reply_markup=user_kb.connect_menu(),
        disable_web_page_preview=True,
    )


_bot_username: str = ""


# ---------------------------------------------------------------------------
# my_chat_member — foydalanuvchi botni bloklaganda/qayta ochganda
# ---------------------------------------------------------------------------
@router.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated) -> None:
    """Bot blok/qayta ochilganda foydalanuvchi yozuvini yangilaydi (jimgina)."""
    user = event.from_user
    if not user:
        return
    new_status = event.new_chat_member.status if event.new_chat_member else ""
    old_status = event.old_chat_member.status if event.old_chat_member else ""
    try:
        await db.upsert_user(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )
    except Exception:  # noqa: BLE001
        logger.debug("my_chat_member: DB yozuvi xato (user=%s)", user.id, exc_info=True)
    logger.info("my_chat_member: user=%s %s -> %s", user.id, old_status, new_status)
