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

import html
import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from app.config import settings
from app.database import db
from app.handlers.admin import ADMIN_PANEL_TEXT
from app.keyboards import user_kb
from app.services import admin_roles, subscriptions
from app.utils import texts
from app.utils.formatting import fmt_number, mention_by_id

router = Router(name="user")
logger = logging.getLogger(__name__)


class PaymentStates(StatesGroup):
    waiting_receipt = State()


def _money(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


def _card_display(value: str) -> str:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return " ".join(digits[i:i + 4] for i in range(0, len(digits), 4)) if digits else "Sozlanmagan"

# ESLATMA (tezlik): har bir callback handler AVVAL ``cb.answer()`` qiladi.


# ---------------------------------------------------------------------------
# /start – asosiy menyu
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Kirish nuqtasi: to'g'ridan-to'g'ri menyu."""
    await state.clear()

    # Admin: chat menyusi o'rniga Web Mini App tugmasi.
    if message.from_user and admin_roles.is_admin(message.from_user.id):
        await message.answer(ADMIN_PANEL_TEXT, reply_markup=user_kb.admin_panel_menu())
        return

    try:
        is_connected = await db.has_active_connection(message.from_user.id)
        premium_visible = (await subscriptions.get_config()).enabled

        if is_connected:
            await message.answer(
                texts.ALREADY_CONNECTED,
                reply_markup=user_kb.main_menu(
                    connected=True, premium_visible=premium_visible
                ),
                disable_web_page_preview=True,
            )
            return

        await message.answer(
            f"{texts.WELCOME}\n\n{texts.MENU_HINT}",
            reply_markup=user_kb.main_menu(
                connected=False, premium_visible=premium_visible
            ),
            disable_web_page_preview=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("cmd_start DB xatosi: %s", e)
        await message.answer(
            texts.WELCOME + "\n\n" + texts.MENU_HINT,
            reply_markup=user_kb.main_menu(connected=False, premium_visible=False),
            disable_web_page_preview=True,
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
    premium_visible = (await subscriptions.get_config()).enabled
    await cb.message.edit_text(
        f"{texts.WELCOME}\n\n{texts.MENU_HINT}",
        reply_markup=user_kb.main_menu(
            connected=connected, premium_visible=premium_visible
        ),
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
        f"👤 Profil: {mention}\n"
        f"🆔 ID: <code>{cb.from_user.id}</code>\n\n"
        f"🔗 Ulanish: {'🟢 ulangan' if stats['active_connections'] else '⚪️ ulanmagan'}\n\n"
        f"📥 Yozib olingan xabarlar: <b>{fmt_number(stats['events_total'])}</b>\n"
        f"✏️ Tahrirlar: <b>{fmt_number(stats['edits'])}</b>\n"
        f"🗑 O'chirishlar: <b>{fmt_number(stats['deletes'] + stats['deletes_media'])}</b>"
    )
    await cb.message.edit_text(
        f"📊 <b>Sizning statistikangiz</b>\n\n{body}",
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
# Premium obuna / qo'lda karta to'lovi
# ---------------------------------------------------------------------------
@router.callback_query(F.data == user_kb.CB_PREMIUM)
async def show_premium(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    cfg = await subscriptions.get_config()
    if not cfg.enabled:
        connected = await db.has_active_connection(cb.from_user.id)
        await cb.message.edit_text(
            "✅ Hozircha Premium rejim yoqilmagan — View Once bepul ishlaydi.",
            reply_markup=user_kb.main_menu(connected=connected, premium_visible=False),
        )
        return

    sub = await subscriptions.status(cb.from_user.id, fresh=True)
    pending = await subscriptions.pending_for_user(cb.from_user.id)
    if sub.get("active"):
        if sub.get("is_lifetime"):
            expiry = "Cheksiz"
            renewal = ""
            show_pay = False
            card_number = ""
        else:
            expiry = str(sub.get("expires_at") or "-").replace("T", " ")[:16]
            renewal = (
                "\n\n⏳ To'lovingiz tekshiruvda." if pending else
                f"\n\nYana {cfg.days} kun qo'shish uchun karta raqamini bosing va to'lov qiling."
            )
            show_pay = pending is None
            card_number = cfg.card_number if show_pay else ""
        text = (
            "💎 <b>Premium obuna</b>\n\n"
            "✅ Holat: <b>ACTIVE</b>\n"
            f"📅 Tugaydi: <b>{expiry}</b>\n"
            f"⏳ Qolgan: <b>{sub.get('remaining_days', 0)} kun</b>\n\n"
            "View Once media'larni <b>?</b> orqali saqlashingiz mumkin."
            + renewal
        )
        await cb.message.edit_text(
            text, parse_mode="HTML",
            reply_markup=user_kb.premium_menu(card_number=card_number, show_pay=show_pay),
        )
        return

    if pending:
        await cb.message.edit_text(
            "⏳ <b>To'lovingiz tekshiruvda</b>\n\n"
            "Chek administratorga yuborilgan. Tasdiqlangandan keyin Premium avtomatik faollashadi.",
            parse_mode="HTML",
            reply_markup=user_kb.premium_menu(card_number="", show_pay=False),
        )
        return

    card = _card_display(cfg.card_number)
    text = (
        "💎 <b>Premium obuna</b>\n\n"
        f"📅 {cfg.days} kun\n"
        f"💰 {_money(cfg.price)} so'm\n\n"
        "💳 <b>To'lov kartasi:</b>\n"
        "Karta raqamini bosib nusxalang 👇\n\n"
        f"<code>{card}</code>\n\n"
        f"👤 Karta egasi: <b>{html.escape(cfg.card_holder or 'Sozlanmagan')}</b>\n\n"
        "To'lov qilganingizdan so'ng <b>✅ To'lov qildim</b> tugmasini bosing va chekni yuboring."
    )
    await cb.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=user_kb.premium_menu(card_number=cfg.card_number, show_pay=True),
    )


@router.callback_query(F.data == user_kb.CB_PAYMENT_START)
async def start_payment_receipt(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    cfg = await subscriptions.get_config()
    if not cfg.enabled:
        await cb.message.answer("Premium rejim hozir o'chirilgan.")
        return
    pending = await subscriptions.pending_for_user(cb.from_user.id)
    if pending:
        await cb.message.edit_text(
            "⏳ To'lovingiz allaqachon tekshiruvda.",
            reply_markup=user_kb.premium_menu(card_number="", show_pay=False),
        )
        return
    await state.set_state(PaymentStates.waiting_receipt)
    await cb.message.edit_text(
        "🧾 <b>To'lov chekini yuboring</b>\n\n"
        "Bank ilovasidan olingan chekni <b>rasm yoki fayl</b> ko'rinishida yuboring.\n\n"
        f"💎 Tarif: Premium — {cfg.days} kun\n"
        f"💰 Summa: {_money(cfg.price)} so'm",
        parse_mode="HTML", reply_markup=user_kb.payment_cancel_menu(),
    )


@router.callback_query(F.data == user_kb.CB_PAYMENT_CANCEL)
async def cancel_payment_receipt(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    connected = await db.has_active_connection(cb.from_user.id)
    cfg = await subscriptions.get_config()
    await cb.message.edit_text(
        f"{texts.WELCOME}\n\n{texts.MENU_HINT}",
        reply_markup=user_kb.main_menu(connected=connected, premium_visible=cfg.enabled),
    )


@router.message(PaymentStates.waiting_receipt)
async def receive_payment_receipt(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    cfg = await subscriptions.get_config()
    if not cfg.enabled:
        await state.clear()
        await message.answer("Premium rejim o'chirilgan.")
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        kind = "photo"
        file_size = int(message.photo[-1].file_size or 0)
    elif message.document:
        mime = str(message.document.mime_type or "").lower()
        filename = str(message.document.file_name or "").lower()
        if mime == "application/pdf" or filename.endswith(".pdf"):
            kind = "pdf"
        elif mime in {"image/jpeg", "image/png", "image/webp"}:
            kind = "image"
        else:
            await message.answer(
                "⚠️ Chek faqat rasm (JPG/PNG/WEBP) yoki PDF bo'lishi mumkin.",
                reply_markup=user_kb.payment_cancel_menu(),
            )
            return
        file_id = message.document.file_id
        file_size = int(message.document.file_size or 0)
    else:
        await message.answer(
            "⚠️ Chekni rasm yoki PDF fayl ko'rinishida yuboring.",
            reply_markup=user_kb.payment_cancel_menu(),
        )
        return

    if file_size > 10 * 1024 * 1024:
        await message.answer(
            "⚠️ Chek hajmi 10 MB dan katta bo'lmasin.",
            reply_markup=user_kb.payment_cancel_menu(),
        )
        return

    pending = await subscriptions.pending_for_user(message.from_user.id)
    if pending:
        await state.clear()
        await message.answer("⏳ To'lovingiz allaqachon tekshiruvda.")
        return

    payment = await subscriptions.create_payment(
        message.from_user.id, receipt_file_id=file_id, receipt_kind=kind
    )
    await state.clear()
    await message.answer(
        "✅ <b>Chekingiz qabul qilindi.</b>\n\n"
        "⏳ To'lov administrator tomonidan tekshirilmoqda.\n"
        "Tasdiqlangandan so'ng Premium avtomatik faollashadi.",
        parse_mode="HTML",
    )

    caption = (
        "🧾 <b>Yangi Premium to'lovi</b>\n\n"
        f"👤 {html.escape(message.from_user.full_name)}\n"
        f"🆔 <code>{message.from_user.id}</code>\n"
        f"💎 {cfg.days} kun\n"
        f"💰 {_money(cfg.price)} so'm\n"
        f"🔑 <code>{payment.get('id')}</code>\n\n"
        "Tasdiqlash/rad etish: Admin Panel → Obuna va to'lovlar"
    )
    for admin_id in sorted(settings.owner_ids):
        try:
            if kind == "photo":
                await message.bot.send_photo(admin_id, file_id, caption=caption, parse_mode="HTML")
            else:
                await message.bot.send_document(admin_id, file_id, caption=caption, parse_mode="HTML")
        except Exception:  # noqa: BLE001
            logger.info("Payment receipt adminga yuborilmadi (admin=%s)", admin_id)


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
