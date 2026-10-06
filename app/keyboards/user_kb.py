"""
Foydalanuvchiga qaratilgan inline klaviaturalar.

Har bir klaviatura kichik funksiya — handlerlar toza qoladi.  Ranglar
(``style=``) va premium emoji ikonkalari (``icon_custom_emoji_id``)
:func:`app.utils.ui.btn` orqali beriladi.

Foydalanuvchilar boshqaruvi va kirish nazorati OLIB TASHLANGAN.
Menyu faqat: Statistika, Ulanish.

Admin panel chatda EMAS — Telegram Web Mini App (TMA) ichida ochiladi.
Adminlar uchun /start va /admin FAQAT «Admin panelni ochish» web_app tugmasini
ko'rsatadi (:func:`admin_panel_menu`).
"""

from __future__ import annotations

import os

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app.emoji_config import EMOJI
from app.utils.ui import BtnStyle, btn, kb

# Callback data prefikslari — handlerlar ham shulardan foydalanadi.
CB_STATS = "user:stats"
CB_CONNECT = "user:connect"
CB_BACK_MENU = "user:menu"

# Web Admin Panel (TMA) manzili — env orqali o'zgartiriladi (kodda qotib qolmasin).
DEFAULT_WEBAPP_URL = "https://res-bot-new-version-production.up.railway.app/"


def webapp_url() -> str:
    """Web Admin Panel manzili (``WEBAPP_URL`` env, bo'lmasa standart)."""
    return (os.getenv("WEBAPP_URL") or DEFAULT_WEBAPP_URL).strip() or DEFAULT_WEBAPP_URL


def admin_panel_menu() -> InlineKeyboardMarkup:
    """Admin uchun yagona tugma: Web Mini App'da panelni ochadi."""
    return kb(
        [
            [
                InlineKeyboardButton(
                    text="🛡 Admin panelni ochish",
                    web_app=WebAppInfo(url=webapp_url()),
                    style=BtnStyle.PRIMARY,
                )
            ]
        ]
    )


def main_menu(*, connected: bool = False) -> InlineKeyboardMarkup:
    """Start menyusi: Statistika, Ulanish."""
    connect_label = (
        f"{EMOJI.menu_connect.fallback} Ulangan ✓"
        if connected
        else f"{EMOJI.menu_connect.fallback} Ulanish"
    )
    rows = [
        [
            btn(
                f"{EMOJI.menu_stats.fallback} Statistika",
                CB_STATS,
                style=BtnStyle.PRIMARY,
                emoji_id=EMOJI.menu_stats.emoji_id,
            )
        ],
        [
            btn(
                connect_label,
                CB_CONNECT,
                style=BtnStyle.SUCCESS if connected else BtnStyle.PRIMARY,
                emoji_id=EMOJI.menu_connect.emoji_id,
            )
        ],
    ]
    return kb(rows)


def back_btn():
    """«Menyuga qaytish» tugmasi — emoji registrydan."""
    return btn(
        f"{EMOJI.menu_back.fallback} Menyuga qaytish",
        CB_BACK_MENU,
        style=BtnStyle.DANGER,
        emoji_id=EMOJI.menu_back.emoji_id,
    )


def connect_menu(bot_username: str = "") -> InlineKeyboardMarkup:
    """«Ulanish» yo'riqnomi ostidagi tugmalar."""
    settings_button = {
        "text": f"{EMOJI.menu_settings.fallback} Sozlamalarni ochish",
        "url": "tg://settings/edit",
        "style": BtnStyle.SUCCESS,
    }
    return kb(
        [
            [InlineKeyboardButton(**settings_button)],
            [back_btn()],
        ]
    )


def back_to_menu() -> InlineKeyboardMarkup:
    return kb([[back_btn()]])
