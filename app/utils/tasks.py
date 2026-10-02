"""
Fon vazifalari (fire-and-forget) — update javobini KUTDIRMAYDIGAN ishlar.

Misol: middleware har bir foydalanuvchi uchun ro'yxatga olish yozuvini
``users`` jadvaliga yozadi.  Supabase uzoqda bo'lsa bu yozuv ~1 soniya
oladi — foydalanuvchi esa javobni kutib turmasligi kerak.  Shuning uchun
yozuv :func:`spawn` orqali FONDA bajariladi.

Muhim: bu faqat "yozib qo'yish" ishlari uchun.  Natijasi darhol kerak
bo'lgan so'rovlar (masalan, ekran ma'lumotlari) ``await`` qilinishi shart.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, Set

logger = logging.getLogger(__name__)

# Vazifalar event loop tomonidan "yig'ib olinmasligi" uchun havola ushlaymiz.
_tasks: Set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str = "bg") -> asyncio.Task:
    """Korutinani FONDA ishga tushiradi va darhol qaytadi.

    Xato yuz bersa log yoziladi, bot esa to'xtamaydi (foydalanuvchi update'i
    allaqachon javob olgan bo'ladi).
    """
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if t.cancelled():
            return
        error = t.exception()
        if error is not None:  # noqa: SIM102 – log bilan "yutamiz"
            logger.error("Fon vazifasi xatosi (%s): %s", name, error, exc_info=error)

    task.add_done_callback(_on_done)
    return task


def pending_count() -> int:
    """Hozir bajarilayotgan fon vazifalari soni (testlar/diagnostika uchun)."""
    return len(_tasks)


async def drain(timeout: float = 10.0) -> None:
    """Kutilayotgan fon vazifalari tugashini kutadi.

    MUHIM: bot to'xtatilganda (yoki baza yopilishidan oldin) chaqirilishi
    kerak — aks holda yarim qolgan yozuv "closed database" xatosini beradi.
    """
    if not _tasks:
        return
    await asyncio.wait(list(_tasks), timeout=timeout)
