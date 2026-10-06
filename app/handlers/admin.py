"""
Admin kirish nuqtasi — panel Telegram Web Mini App (TMA) ichida ochiladi.

Chatdagi inline-tugmali admin panel OLIB TASHLANGAN.  Bu yerda faqat:

* ``/admin``  — adminga Web Mini App ochadigan yagona tugma;
* ``/start``  — admin uchun ham shu (see :mod:`app.handlers.user`).

XAVFSIZLIK: tugma faqat adminlarga ko'rsatiladi, lekin bu FAQAT UX.  Haqiqiy
himoya serverda — :mod:`app.web` har bir so'rovda Telegram ``initData``
imzosini va rolni qayta tekshiradi.
"""

from __future__ import annotations

from app.emoji_config import EMOJI

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.keyboards import user_kb
from app.services import admin_roles

router = Router(name="admin")
logger = logging.getLogger(__name__)

ADMIN_PANEL_TEXT = (
    f"{EMOJI.admin_panel.tag} <b>ADMIN PANEL</b>\n\n"
    "Boshqaruv paneli Web Mini App ichida ochiladi — pastdagi tugmani bosing."
)


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    """Adminga Web Mini App tugmasini yuboradi (boshqalarga — hech narsa)."""
    user = message.from_user
    if not user or not admin_roles.is_admin(user.id):
        logger.info("/admin: ruxsatsiz urinish (user=%s)", getattr(user, "id", None))
        return
    await message.answer(ADMIN_PANEL_TEXT, reply_markup=user_kb.admin_panel_menu())
