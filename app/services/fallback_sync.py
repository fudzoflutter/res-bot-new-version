"""SQLite fallback -> PRIMARY (Postgres) reconciliation.

MUAMMO
------
Supabase ishga tushish paytida javob bermasa, bot LOCAL SQLite'da ishlashda
davom etadi (``Database._init_sqlite(fallback=True)``) va shu paytda yozilgan
foydalanuvchi/xabar/log ma'lumotlari faqat SQLite faylida qoladi.  Supabase
tiklangach ular AVTOMATIK ko'chirilmasdi — ma'lumot YO'QOLARDI.

YECHIM (kursorli, idempotent ko'chirish)
----------------------------------------
* Manba SQLite'dagi yozuvlar tartib raqamiga ega (``events.id``,
  ``activity_log.id``) — bu tabiiy kursor.
* Kursorlar (``events``/``activity_log`` uchun oxirgi ko'chirilgan id) va
  ``users``/``connections``/``bot_settings`` (idempotent UPSERT) BATCH bilan
  birgalikda PRIMARY bazada BITTA tranzaksiyada qo'llanadi
  (``target.apply_sync_batch``).  Shuning uchun jarayon to'satdan o'lsa ham
  "yozuvlar yozildi, lekin kursor saqlanmadi" holati bo'lishi mumkin emas —
  qayta urinish hech qachon dublikat yaratmaydi va hech narsani yo'qotmaydi.
* ``sync_generation`` — manba SQLite fayli qayta yaratilsa (yangi fayl),
  eski kursorlar yangi faylga noto'g'ri qo'llanmasligi uchun avlod belgisi.

Modul ataylab ``app.database`` ni import QILMAYDI (aylanma importdan qochish):
u faqat ``target`` obyektning ``sync_cursors()`` va ``apply_sync_batch()``
metodlarini chaqiradi.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from app.config import settings
from app.utils.timeutils import now_iso

logger = logging.getLogger(__name__)

# PRIMARY bazada saqlanadigan kursor kaliti (JSON qiymat).
SYNC_CURSORS_KEY = "sqlite_sync_cursors"
# Manba SQLite faylining "avlod" identifikatori.
SYNC_GENERATION_KEY = "sync_generation"
# Ushbu kalitlar PRIMARY bazaga KO'CHIRILMAYDI (ichki xizmat metadatasi).
_EXCLUDED_SETTING_KEYS = {SYNC_GENERATION_KEY, SYNC_CURSORS_KEY}

_EMPTY_STATS = {
    "users": 0,
    "connections": 0,
    "settings": 0,
    "subscriptions": 0,
    "payment_requests": 0,
    "events": 0,
    "activity_log": 0,
}


async def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    """So'rovni bajaradi; jadval/ustun yo'q bo'lsa bo'sh ro'yxat qaytaradi."""
    try:
        cursor = await conn.execute(sql, params)
    except Exception:  # noqa: BLE001 – eski bot.db da jadval bo'lmasligi mumkin
        return []
    try:
        return [dict(row) for row in await cursor.fetchall()]
    finally:
        await cursor.close()


async def _max_id(conn, table: str) -> int:
    rows = await _rows(conn, f"SELECT COALESCE(MAX(id), 0) AS m FROM {table}")
    if not rows:
        return 0
    try:
        return int(rows[0].get("m") or 0)
    except (TypeError, ValueError):
        return 0


async def _source_generation(conn) -> str:
    """Manba fayl uchun avlod identifikatorini oladi (kerak bo'lsa yaratadi)."""
    rows = await _rows(
        conn, "SELECT value FROM bot_settings WHERE key = ?", (SYNC_GENERATION_KEY,)
    )
    if rows and rows[0].get("value"):
        return str(rows[0]["value"])
    generation = uuid.uuid4().hex
    try:
        await conn.execute(
            "INSERT INTO bot_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (SYNC_GENERATION_KEY, generation, now_iso()),
        )
        await conn.commit()
    except Exception:  # noqa: BLE001 – yozib bo'lmasa avlod barqaror bo'lmaydi
        logger.debug("sync_generation yozilmadi", exc_info=True)
        return ""
    return generation


async def _open_source(path: Optional[str] = None):
    """Manba SQLite faylini ochadi (yo'q bo'lsa ``None``)."""
    source = Path(path or settings.db_path)
    if not source.exists():
        return None
    import aiosqlite

    conn = await aiosqlite.connect(str(source))
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA busy_timeout=5000")
    except Exception:  # noqa: BLE001
        pass
    return conn


async def seed_cursors_at_high_water(target, *, path: Optional[str] = None) -> dict:
    """Birinchi sinxronizatsiya uchun kursorlarni JORIY yuqori nuqtaga qo'yadi.

    ``_import_sqlite_once`` (eski bir martalik import) allaqachon mavjud
    yozuvlarni ko'chirgan bo'ladi.  Shu sababli kursorlar joriy maksimal id
    ustiga qo'yiladi — o'sha yozuvlar IKKINCHI MARTA ko'chirilmaydi
    (dublikat bo'lmaydi).  Bundan keyingi barcha yangi yozuvlar kursor bo'yicha
    ko'chiriladi.
    """
    conn = await _open_source(path)
    if conn is None:
        return {}
    try:
        cursors = {
            "generation": await _source_generation(conn),
            "events": await _max_id(conn, "events"),
            "activity_log": await _max_id(conn, "activity_log"),
        }
        await target.apply_sync_batch(cursors, {})
        logger.info("SQLite sync kursorlari o'rnatildi: %s", cursors)
        return cursors
    finally:
        await conn.close()


async def sync_sqlite_into(
    target, *, path: Optional[str] = None, batch_size: int = 500
) -> dict:
    """Manba SQLite'dagi YANGI yozuvlarni PRIMARY bazaga ko'chiradi.

    Idempotent: kursorlar PRIMARY bazada saqlanadi, shuning uchun takroriy
    chaqiruv hech qanday dublikat yaratmaydi va ma'lumot yo'qolmaydi.
    """
    stats = dict(_EMPTY_STATS)
    conn = await _open_source(path)
    if conn is None:
        return stats
    try:
        generation = await _source_generation(conn)
        try:
            cursors = dict(await target.sync_cursors())
        except Exception:  # noqa: BLE001
            logger.debug("sync kursorlarini o'qib bo'lmadi", exc_info=True)
            cursors = {}
        if str(cursors.get("generation") or "") != generation:
            # Yangi avlod (fayl qayta yaratildi) — noldan boshlaymiz.
            cursors = {"generation": generation, "events": 0, "activity_log": 0}

        ev_cursor = int(cursors.get("events") or 0)
        lg_cursor = int(cursors.get("activity_log") or 0)
        limit = max(1, int(batch_size))

        # users/connections/settings — idempotent UPSERT (kam hajmli).
        static = {
            "users": await _rows(conn, "SELECT * FROM users"),
            "connections": await _rows(conn, "SELECT * FROM connections"),
            "settings": [
                row
                for row in await _rows(conn, "SELECT * FROM bot_settings")
                if str(row.get("key")) not in _EXCLUDED_SETTING_KEYS
            ],
            "subscriptions": await _rows(conn, "SELECT * FROM subscriptions"),
            "payment_requests": await _rows(conn, "SELECT * FROM payment_requests"),
        }

        static_applied = False
        while True:
            events = await _rows(
                conn,
                "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?",
                (ev_cursor, limit),
            )
            logs = await _rows(
                conn,
                "SELECT * FROM activity_log WHERE id > ? ORDER BY id ASC LIMIT ?",
                (lg_cursor, limit),
            )
            is_last = not events and not logs
            if is_last and static_applied:
                break
            if events:
                ev_cursor = max(int(row["id"]) for row in events)
            if logs:
                lg_cursor = max(int(row["id"]) for row in logs)

            new_cursors = {
                "generation": generation,
                "events": ev_cursor,
                "activity_log": lg_cursor,
            }
            batch = {
                "users": static["users"],
                "connections": static["connections"],
                "settings": static["settings"],
                "subscriptions": static["subscriptions"],
                "payment_requests": static["payment_requests"],
                "events": events,
                "activity_log": logs,
            }
            await target.apply_sync_batch(new_cursors, batch)
            cursors = new_cursors

            stats["users"] += len(batch["users"])
            stats["connections"] += len(batch["connections"])
            stats["settings"] += len(batch["settings"])
            stats["subscriptions"] += len(batch["subscriptions"])
            stats["payment_requests"] += len(batch["payment_requests"])
            stats["events"] += len(events)
            stats["activity_log"] += len(logs)

            static = {
                "users": [], "connections": [], "settings": [],
                "subscriptions": [], "payment_requests": [],
            }
            static_applied = True
            if is_last:
                break
        return stats
    finally:
        await conn.close()


def cursors_from_json(raw: Any) -> dict:
    """Yordamchi: saqlangan JSON kursorni xavfsiz dict ga aylantiradi."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}
