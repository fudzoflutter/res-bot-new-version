"""
Outer middleware (har bir update uchun ishlaydi).

:class:`RegisterUserMiddleware` faqat bitta ish qiladi:

* **Ro'yxatga olish** — foydalanuvchini DBga yozish va ``last_activity``
  ni yangilash (texnik statistika uchun).

Kirish nazorati (allow/deny/ban) butunlay OLIB TASHLANGAN.
Admin panelga kirish owner/admin/moderator/viewer rollari bilan server-side
tekshiriladi. Oddiy foydalanuvchilar monitoring ma'lumotlariga umuman
kira olmaydi — faqat tugmani yashirish YETARLI EMAS.

HOLD (texnik xizmat) REJIMI
---------------------------
Hold mode YOQILGANDA oddiy foydalanuvchi komandasi HANDLER GA YETIB
BORMAYDI: shu yerda (eng tashqi qavatda) to'xtatiladi va foydalanuvchiga
texnik xizmat xabari yuboriladi. ADMIN/OWNER hech qachon to'xtatilmaydi —
admin panel, healthcheck va boshqaruv doim ishlaydi. Foydalanuvchini DBga
ro'yxatga olish esa Hold vaqtida ham bajariladi; shu sabab yangi userlar
Users/Analytics'da yo'qolib qolmaydi.

BUSINESS (biznes-bot) update'lari bu yerda to'xtatilmaydi: ular oddiy
foydalanuvchi amali emas, balki ulangan chatlardan kelayotgan hisobotlar —
to'xtatilsa kuzatuv ma'lumotlari YO'QOLADI.  Yangi biznes-ulanish esa
:mod:`app.handlers.business` ichida hold vaqtida rad etiladi.

TEZLIK
------
DB yozuvi FONDA ketadi: Supabase uzoqda bo'lsa bu ~1.2 s, lekin
foydalanuvchi uni KUTMAYDI (60 sekundda bir marta).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware
from aiogram.types import (
    CallbackQuery,
    Message,
    TelegramObject,
    Update,
    User as TgUser,
)

from app.database import db
from app.services import admin_roles, maintenance, moderation
from app.utils.tasks import spawn

logger = logging.getLogger(__name__)

# Tezlik: har bir update uchun DB YOZUVI juda qimmat.
# Yangi foydalanuvchini ro'yxatga olish + last_activity
# yangilash 60 sekundda BIR marta yetarli.
UPSERT_INTERVAL_SECONDS = 60.0

# Oddiy anti-spam: bir foydalanuvchi soniyasiga ko'pi bilan N so'rov.
RATE_LIMIT_EVENTS = 5
RATE_LIMIT_CALLBACKS = 15
RATE_WINDOW = 1.0  # sekund

# Keshlar (protsess ichida) — faqat event loop ichidan o'zgartiriladi.
_last_upsert: dict[int, float] = {}
_buckets: dict[int, list[float]] = {}
_MAX_BUCKETS = 5_000

# ADMIN_ID import qilmaymiz (circular import xavfi) — admin tekshiruvi
# handler darajasida amalga oshiriladi (app/services/admin_roles.py va app/web.py).


class RegisterUserMiddleware(BaseMiddleware):
    """Foydalanuvchini DBga yozish (fon yozuvi — tezlikka ta'sir qilmaydi).

    MUHIM: middleware ``dp.update`` darajasida o'rnatiladi
    (:mod:`app.main`), ya'ni bu yerga keladigan ``event`` — **Update**,
    Message/CallbackQuery emas.  Shu sababli avval uning ICHIDAGI
    foydalanuvchi event'i ajratib olinadi (:meth:`_direct_event`):

    * ``message`` / ``callback_query`` — egasi foydalanuvchi, ro'yxatga olinadi;
    * ``business_*`` update'lari ATAYIN o'tkazib yuboriladi — ularda
      ``from_user`` — suhbatdosh, bot foydalanuvchisi emas (ro'yxatni
      ifloslantiradi).

    Kirish nazorati bu yerda TEKSHIRILMAYDI — bu handlers vakolati.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: Optional[TgUser] = data.get("event_from_user")
        direct = self._direct_event(event)

        if user is not None and not user.is_bot and direct is not None:
            # Admin tekshiruvi (hold va rate limit uchun umumiy).
            # MUHIM: faqat env'dagi ``settings.admin_ids`` EMAS — panel orqali
            # runtime qo'shilgan rollar (admin/moderator/viewer) ham hisobga
            # olinadi, aks holda ular hold rejimida bloklanib qolardi.
            is_admin_user = admin_roles.is_admin(user.id)

            # ---- ANTI-SPAM (rate limit) ----------------------------------
            # Hold rejimidan OLDIN turadi: texnik xizmat vaqtida ham spamni
            # to'xtatish kerak (aks holda har bir so'rovga javob yoziladi).
            limit = (
                RATE_LIMIT_EVENTS
                if isinstance(direct, Message)
                else RATE_LIMIT_CALLBACKS
            )
            if not is_admin_user and self._is_flooding(user.id, limit):
                logger.warning("Rate limit hit for user %s", user.id)
                return None

            # ---- MODERATSIYA (ban) --------------------------------------
            # Ban ro'yxatidagi foydalanuvchi update'i HANDLERGA YETIB
            # BORMAYDI.  Server-side tekshiruv (ruxsat: users.moderate
            # orqali ban qo'yiladi).  Adminlar hech qachon bloklanmaydi.
            if not is_admin_user and moderation.is_banned(user.id):
                logger.info("Ban qilingan foydalanuvchi: %s", user.id)
                await self._announce_banned(direct)
                return None

            # ---- RO'YXATGA OLISH (fon) -----------------------------------
            # HOLD tekshiruvidan OLDIN yozamiz. Yangi user aynan texnik xizmat
            # paytida /start qilsa ham Users/Analytics ro'yxatiga kiradi va
            # Hold o'chirilganda umumiy "bot yana ishga tushdi" xabarini oladi.
            # TEZLIK: ikki qavat himoya.
            #  1) upsert har update'da emas — 60 s da bir marta;
            #  2) yozuv FONDA ketadi: foydalanuvchi kutmaydi.
            # ``time.monotonic()`` boot vaqtiga bog'liq (yangi mashinada < 60 s
            # bo'lishi mumkin), shuning uchun "hech qachon yozilmagan" holati
            # ``None`` bilan ifodalanadi (0.0 emas!) — yangi foydalanuvchi
            # birinchi update'dayoq ro'yxatga olinadi.
            now = time.monotonic()
            last = _last_upsert.get(user.id)
            if last is None or now - last >= UPSERT_INTERVAL_SECONDS:
                _last_upsert[user.id] = now
                if len(_last_upsert) > _MAX_BUCKETS:
                    oldest = next(iter(_last_upsert))
                    _last_upsert.pop(oldest, None)
                spawn(
                    self._register_user(user),
                    name=f"upsert:{user.id}",
                )

            # ---- HOLD (texnik xizmat) REJIMI -----------------------------
            # Adminlar bundan MUSTASNO — ular panelni boshqara olishi shart.
            # Oddiy user handlerga o'tmaydi, lekin yuqoridagi yengil fon upsert
            # tufayli yangi user ro'yxatdan yo'qolib qolmaydi.
            if not is_admin_user and await maintenance.is_enabled():
                logger.info("Hold mode: user %s update'i to'xtatildi", user.id)
                await self._announce_hold(direct)
                return None

        return await handler(event, data)

    @staticmethod
    async def _register_user(user: TgUser) -> None:
        """Persist a user; if the write finally fails, allow immediate retry.

        The throttle timestamp is set before spawning to prevent duplicate writes.
        Without clearing it on failure, a brand-new user could disappear from the
        admin Users/Analytics screens for up to 60 seconds after a transient DB error.
        """
        try:
            await db.upsert_user(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
            )
        except Exception:
            _last_upsert.pop(int(user.id), None)
            raise

    @staticmethod
    async def _announce_hold(direct: TelegramObject) -> None:
        """Foydalanuvchiga texnik xizmat xabarini yuborish (xato bo'lsa jim)."""
        from app.utils import texts

        try:
            if isinstance(direct, CallbackQuery):
                await direct.answer(
                    "🔧 Bot texnik xizmatda. Iltimos, keyinroq urinib ko'ring.",
                    show_alert=True,
                )
            elif isinstance(direct, Message):
                await direct.answer(texts.HOLD_MODE, parse_mode="HTML")
        except Exception:  # noqa: BLE001 – xabar ketmasa ham bot ishlashda davom
            logger.debug("Hold mode xabari yuborilmadi", exc_info=True)

    @staticmethod
    async def _announce_banned(direct: TelegramObject) -> None:
        """Ban qilingan foydalanuvchiga qisqa sabab (xato bo'lsa jim)."""
        text = "🚫 Siz botdan foydalanishdan bloklangansiz."
        try:
            if isinstance(direct, CallbackQuery):
                await direct.answer(text, show_alert=True)
            elif isinstance(direct, Message):
                await direct.answer(text)
        except Exception:  # noqa: BLE001
            logger.debug("Ban xabari yuborilmadi", exc_info=True)

    @staticmethod
    def _direct_event(event: TelegramObject) -> Optional[TelegramObject]:
        """Update ichidan foydalanuvchi bilan BEVOSITA bog'liq event'ni oladi.

        ``None`` — bu update bo'yicha ro'yxatga olish kerak emas
        (biznes-hodisalar hamda xabar/callback bo'lmagan update turlari).
        """
        inner: Optional[TelegramObject] = event
        if isinstance(event, Update):
            kind = event.event_type or ""
            if kind.startswith("business_"):
                return None
            inner = getattr(event, kind, None)

        if isinstance(inner, CallbackQuery):
            return inner
        if isinstance(inner, Message) and not inner.business_connection_id:
            return inner
        return None

    @staticmethod
    def _is_flooding(user_id: int, limit: int = RATE_LIMIT_EVENTS) -> bool:
        if len(_buckets) > _MAX_BUCKETS:
            oldest = next(iter(_buckets))
            _buckets.pop(oldest, None)
        now = time.monotonic()
        window: list[float] = _buckets.setdefault(user_id, [])
        window[:] = [t for t in window if now - t < RATE_WINDOW]
        window.append(now)
        return len(window) > limit
