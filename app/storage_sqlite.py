"""
Zaxira (fallback) saqlash qatlami – mahalliy SQLite.

Supabase sozlanmagan bo'lsa (``.env`` da SUPABASE_DB_URL yo'q), bot shu
backendda to'liq ishlaydi.  URL qo'shilishi bilan keyingi ishga tushirishda
bot avtomatik Supabasega o'tadi va mavjud bot.db ma'lumotlarini bir marta
import qiladi (app/database.py dagi Postgres klassiga qarang).

Metodlar va qaytariladigan ma'lumot shakllari Postgres versiyasi bilan
AYNAN bir xil — handlerlar farqni sezmaydi.

Bot jadvallari: ``users``, ``events``, ``connections``, ``activity_log``,
``bot_settings`` va ``instance_lock``.  Foydalanuvchi kirish tizimi
(ruxsat / rad / ban) OLIB TASHLANGAN.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

import aiosqlite

from app.config import settings
from app.database import (
    _broadcast_counts_from_rows,
    _resilient,
    _search_filters,
    _severity_levels,
    analytics_overview_sql,
    analytics_row,
    analytics_series_sql,
)
from app.services.fallback_sync import SYNC_CURSORS_KEY
from app.utils.timeutils import now_iso, local_now

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    username       TEXT,
    first_name     TEXT,
    last_name      TEXT,
    connected_at   TEXT,
    last_activity  TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    sender_id    INTEGER,
    business_connection_id TEXT,
    chat_id      INTEGER,
    chat_title   TEXT,
    event_type   TEXT NOT NULL,
    message_id   INTEGER,
    details      TEXT NOT NULL DEFAULT '',
    occurred_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
    business_connection_id TEXT PRIMARY KEY,
    user_id                INTEGER NOT NULL,
    user_chat_id           INTEGER,
    is_enabled             INTEGER NOT NULL DEFAULT 1,
    connected_at           TEXT,
    disconnected_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_time   ON events (occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_type   ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_user   ON events (user_id);
CREATE INDEX IF NOT EXISTS idx_events_chat_message ON events (chat_id, message_id);
-- Retention / analytics / qidiruv: tur va vaqt bo'yicha birgalikda.
CREATE INDEX IF NOT EXISTS idx_events_type_time ON events (event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_user_time ON events (user_id, occurred_at);

CREATE TABLE IF NOT EXISTS instance_lock (
    id           TEXT PRIMARY KEY,
    instance     TEXT NOT NULL,
    host         TEXT,
    pid          INTEGER,
    service      TEXT,
    deployment   TEXT,
    started_at   TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT NOT NULL,
    connection_id   TEXT,
    user_id         INTEGER,
    description     TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'INFO',
    occurred_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_log_time ON activity_log (occurred_at);
CREATE INDEX IF NOT EXISTS idx_activity_log_type ON activity_log (event_type);
CREATE INDEX IF NOT EXISTS idx_activity_log_conn ON activity_log (connection_id);
CREATE INDEX IF NOT EXISTS idx_connections_user  ON connections (user_id);

-- Broadcast progressi: har bir oluvchi uchun alohida holat. UNKNOWN holati
-- qayta yuborish xavfsizligini saqlash uchun retry qilinmaydi.
CREATE TABLE IF NOT EXISTS broadcast_recipients (
    broadcast_id TEXT NOT NULL,
    user_id      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'PENDING',
    error        TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (broadcast_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_status
    ON broadcast_recipients (broadcast_id, status);
"""


class SqliteDatabase:
    """Local SQLite backend (fallback).  Same API as the Postgres one."""

    def __init__(self) -> None:
        self._conn: Optional[aiosqlite.Connection] = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not initialised - call init() first")
        return self._conn

    async def init(self) -> None:
        self._conn = await aiosqlite.connect(settings.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA)
        # Migratsiya: eski bot.db da 'sender_id' ustuni bo'lmasligi mumkin
        # (xabar KIMdan kelganini saqlash — o'z-o'zini o'chirish uchun).
        cursor = await self._conn.execute("PRAGMA table_info(events)")
        event_cols = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        if "sender_id" not in event_cols:
            await self._conn.execute("ALTER TABLE events ADD COLUMN sender_id INTEGER")
        if "business_connection_id" not in event_cols:
            await self._conn.execute(
                "ALTER TABLE events ADD COLUMN business_connection_id TEXT"
            )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_connection_message "
            "ON events (business_connection_id, chat_id, message_id)"
        )
        # Migratsiya: deploy (Railway) identifikatorlari — yangi deploy eski
        # deploy qulfini xavfsiz egallashi uchun (instance_lock.py).
        cursor = await self._conn.execute("PRAGMA table_info(instance_lock)")
        lock_cols = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        if "service" not in lock_cols:
            await self._conn.execute("ALTER TABLE instance_lock ADD COLUMN service TEXT")
        if "deployment" not in lock_cols:
            await self._conn.execute("ALTER TABLE instance_lock ADD COLUMN deployment TEXT")
        await self._conn.commit()
        logger.info("SQLite ready at %s (Supabase not configured)", settings.db_path)


    # ======================================================================
    # ACTIVITY LOG
    # ======================================================================

    async def add_activity_log(
        self,
        event_type: str,
        description: str,
        *,
        connection_id=None,
        user_id=None,
        severity: str = "INFO",
    ) -> None:
        await self._execute(
            """
            INSERT INTO activity_log
                (event_type, connection_id, user_id, description, severity, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_type, connection_id, user_id, description, severity, now_iso()),
        )

    async def recent_activity_logs(self, limit: int = 50) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
        )

    async def count_activity_logs(self) -> int:
        return int(await self._fetchval("SELECT COUNT(*) FROM activity_log") or 0)

    async def logs_page_count(self, *, min_severity: str | None = None) -> int:
        """Filtered count matching logs_page() conditions."""
        if min_severity:
            levels = _severity_levels(min_severity)
            placeholders = ", ".join("?" for _ in levels)
            value = await self._fetchval(
                f"SELECT COUNT(*) FROM activity_log WHERE severity IN ({placeholders})",
                tuple(levels),
            )
        else:
            value = await self._fetchval("SELECT COUNT(*) FROM activity_log")
        return int(value or 0)

    async def prune_activity_logs(self, keep: int = 10_000) -> None:
        await self._execute(
            """
            DELETE FROM activity_log
            WHERE id NOT IN (
                SELECT id FROM activity_log ORDER BY id DESC LIMIT ?
            )
            """,
            (keep,),
        )

    async def logs_page(
        self,
        limit: int = 10,
        offset: int = 0,
        min_severity: Optional[str] = None,
    ) -> list[dict]:
        """Activity log sahifasi (ixtiyoriy: faqat WARNING+)."""
        if min_severity:
            levels = _severity_levels(min_severity)
            placeholders = ", ".join("?" for _ in levels)
            return await self._fetch_all(
                f"""
                SELECT * FROM activity_log
                WHERE severity IN ({placeholders})
                ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (*levels, max(1, int(limit)), max(0, int(offset))),
            )
        return await self._fetch_all(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT ? OFFSET ?",
            (max(1, int(limit)), max(0, int(offset))),
        )

    async def prune_activity_logs_older_than(self, days: int) -> int:
        """Retention: ``days`` kundan eski loglarni o'chiradi."""
        cutoff = (local_now() - timedelta(days=max(1, int(days)))).isoformat(
            timespec="seconds"
        )
        return await self._run_rowcount(
            "DELETE FROM activity_log WHERE occurred_at < ?", (cutoff,)
        )

    # ======================================================================
    # BOT SETTINGS
    # ======================================================================

    async def get_setting(self, key: str, default: str = "") -> str:
        value = await self._fetchval(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        )
        return str(value) if value is not None else default

    async def set_setting(self, key: str, value: str) -> None:
        await self._execute(
            """
            INSERT INTO bot_settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now_iso()),
        )

    # ======================================================================
    # CONNECTIONS EXTENDED
    # ======================================================================

    async def all_connections(self) -> list[dict]:
        return await self._fetch_all(
            """
            SELECT c.*,
                   u.username   AS owner_username,
                   u.first_name AS owner_first_name
            FROM connections c
            LEFT JOIN users u ON u.user_id = c.user_id
            ORDER BY c.connected_at DESC
            """
        )

    async def count_connections(self, enabled_only: bool = False) -> int:
        if enabled_only:
            value = await self._fetchval(
                "SELECT COUNT(*) FROM connections WHERE is_enabled = 1"
            )
        else:
            value = await self._fetchval("SELECT COUNT(*) FROM connections")
        return int(value or 0)

    async def get_connection_stats(self, connection_id: str) -> dict:
        row = await self._fetch_one(
            """
            SELECT
              (SELECT COUNT(*) FROM events e
               WHERE (e.business_connection_id = c.business_connection_id
                      OR (e.business_connection_id IS NULL AND e.user_id = c.user_id))
                 AND e.event_type NOT IN ('edit','delete','delete_media','connection'))
                                                      AS received,
              (SELECT COUNT(*) FROM events e
               WHERE (e.business_connection_id = c.business_connection_id
                      OR (e.business_connection_id IS NULL AND e.user_id = c.user_id))
                 AND e.event_type = 'edit')            AS edited,
              (SELECT COUNT(*) FROM events e
               WHERE (e.business_connection_id = c.business_connection_id
                      OR (e.business_connection_id IS NULL AND e.user_id = c.user_id))
                 AND e.event_type IN ('delete','delete_media')) AS deleted
            FROM connections c
            WHERE c.business_connection_id = ?
            """,
            (connection_id,),
        )
        row = row or {}
        return {
            "received": int(row.get("received") or 0),
            "edited": int(row.get("edited") or 0),
            "deleted": int(row.get("deleted") or 0),
        }

    async def all_connections_with_stats(self) -> list[dict]:
        """Barcha ulanishlar + hisoblar — BITTA so'rov (Postgres bilan bir xil)."""
        return await self._fetch_all(
            """
            SELECT c.*,
                   u.username      AS owner_username,
                   u.first_name    AS owner_first_name,
                   u.last_activity AS owner_last_activity,
                   COALESCE(s.received, 0)  AS received_count,
                   COALESCE(s.edited, 0)    AS edited_count,
                   COALESCE(s.deleted, 0)   AS deleted_count,
                   s.last_event_at          AS last_event_at,
                   s.last_message_at        AS last_message_at
            FROM connections c
            LEFT JOIN users u ON u.user_id = c.user_id
            LEFT JOIN (
                SELECT
                    user_id,
                    SUM(CASE WHEN event_type NOT IN
                        ('edit','delete','delete_media','connection')
                        THEN 1 ELSE 0 END)                        AS received,
                    SUM(CASE WHEN event_type = 'edit'
                        THEN 1 ELSE 0 END)                        AS edited,
                    SUM(CASE WHEN event_type IN ('delete','delete_media')
                        THEN 1 ELSE 0 END)                        AS deleted,
                    MAX(occurred_at)                              AS last_event_at,
                    MAX(CASE WHEN message_id IS NOT NULL
                        THEN occurred_at END)                     AS last_message_at
                FROM events
                GROUP BY user_id
            ) s ON s.user_id = c.user_id
            ORDER BY c.connected_at DESC
            """
        )

    async def delete_connection(self, connection_id: str) -> bool:
        """Ulanish yozuvini o'chiradi (owner-only amal, tasdiqdan keyin)."""
        changed = await self._run_rowcount(
            "DELETE FROM connections WHERE business_connection_id = ?",
            (connection_id,),
        )
        return changed > 0

    # ======================================================================
    # FALLBACK SINXRONIZATSIYA (SQLite -> primary)
    # ======================================================================

    async def sync_cursors(self) -> dict:
        """Saqlangan sinxronizatsiya kursorlari (yo'q bo'lsa bo'sh dict)."""
        raw = await self.get_setting(SYNC_CURSORS_KEY, "")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    async def apply_sync_batch(self, cursors: dict, batch: dict) -> None:
        """Fallback batchini ATOMAR qo'llaydi va kursorlarni saqlaydi.

        Barcha yozuvlar + kursor BITTA tranzaksiyada commit qilinadi — jarayon
        to'satdan o'lsa ham "yozuvlar bor, kursor yo'q" holati bo'lmaydi.
        """
        conn = self.conn
        try:
            for u in batch.get("users") or ():
                await conn.execute(
                    """
                    INSERT INTO users (user_id, username, first_name, last_name,
                                       connected_at, last_activity, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        username   = excluded.username,
                        first_name = excluded.first_name,
                        last_name  = excluded.last_name,
                        connected_at  = COALESCE(excluded.connected_at, users.connected_at),
                        last_activity = COALESCE(excluded.last_activity, users.last_activity)
                    WHERE COALESCE(excluded.last_activity, '')
                          > COALESCE(users.last_activity, '')
                    """,
                    (
                        u.get("user_id"), u.get("username"), u.get("first_name"),
                        u.get("last_name"), u.get("connected_at"),
                        u.get("last_activity"), u.get("created_at") or now_iso(),
                    ),
                )
            for c in batch.get("connections") or ():
                await conn.execute(
                    """
                    INSERT INTO connections (business_connection_id, user_id,
                        user_chat_id, is_enabled, connected_at, disconnected_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(business_connection_id) DO UPDATE SET
                        user_id         = excluded.user_id,
                        user_chat_id    = COALESCE(excluded.user_chat_id, connections.user_chat_id),
                        is_enabled      = excluded.is_enabled,
                        connected_at    = excluded.connected_at,
                        disconnected_at = excluded.disconnected_at
                    WHERE MAX(COALESCE(excluded.connected_at, ''),
                              COALESCE(excluded.disconnected_at, ''))
                          > MAX(COALESCE(connections.connected_at, ''),
                                COALESCE(connections.disconnected_at, ''))
                    """,
                    (
                        c.get("business_connection_id"), c.get("user_id"),
                        c.get("user_chat_id"), 1 if c.get("is_enabled") else 0,
                        c.get("connected_at"), c.get("disconnected_at"),
                    ),
                )
            for s in batch.get("settings") or ():
                await conn.execute(
                    """
                    INSERT INTO bot_settings (key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value, updated_at = excluded.updated_at
                    WHERE excluded.updated_at > bot_settings.updated_at
                    """,
                    (s.get("key"), s.get("value"), s.get("updated_at") or now_iso()),
                )
            for e in batch.get("events") or ():
                await conn.execute(
                    """
                    INSERT INTO events (user_id, sender_id, chat_id, chat_title,
                                        event_type, message_id, details, occurred_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        e.get("user_id"), e.get("sender_id"), e.get("chat_id"),
                        e.get("chat_title"), e.get("event_type"), e.get("message_id"),
                        e.get("details") or "", e.get("occurred_at") or now_iso(),
                    ),
                )
            for row in batch.get("activity_log") or ():
                await conn.execute(
                    """
                    INSERT INTO activity_log (event_type, connection_id, user_id,
                                              description, severity, occurred_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.get("event_type"), row.get("connection_id"),
                        row.get("user_id"), row.get("description") or "",
                        row.get("severity") or "INFO",
                        row.get("occurred_at") or now_iso(),
                    ),
                )
            await conn.execute(
                """
                INSERT INTO bot_settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at
                """,
                (SYNC_CURSORS_KEY, json.dumps(cursors), now_iso()),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    # ======================================================================
    # BROADCAST PROGRESSI (reklama) + ANALYTICS
    # ======================================================================

    async def all_user_ids(
        self, limit: Optional[int] = None, offset: int = 0
    ) -> list[int]:
        """Faqat IDlar (batching) — Postgres backend bilan bir xil."""
        if limit is None:
            rows = await self._fetch_all(
                "SELECT user_id FROM users ORDER BY user_id ASC"
            )
        else:
            rows = await self._fetch_all(
                "SELECT user_id FROM users ORDER BY user_id ASC LIMIT ? OFFSET ?",
                (max(1, int(limit)), max(0, int(offset))),
            )
        return [int(row["user_id"]) for row in rows]

    async def broadcast_seed(self, broadcast_id: str, user_ids: list[int]) -> int:
        """Oluvchilarni PENDING holatida yozadi (idempotent)."""
        if not user_ids:
            return 0
        stamp = now_iso()

        async def run() -> int:
            await self.conn.executemany(
                """
                INSERT OR IGNORE INTO broadcast_recipients
                    (broadcast_id, user_id, status, error, updated_at)
                VALUES (?, ?, 'PENDING', '', ?)
                """,
                [(broadcast_id, int(uid), stamp) for uid in user_ids],
            )
            await self.conn.commit()
            return len(user_ids)

        return int(await _resilient("broadcast_seed", run) or 0)

    async def broadcast_mark(
        self, broadcast_id: str, user_id: int, status: str, error: str = ""
    ) -> None:
        await self._execute(
            """
            INSERT INTO broadcast_recipients
                (broadcast_id, user_id, status, error, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(broadcast_id, user_id) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (broadcast_id, int(user_id), str(status), str(error)[:200], now_iso()),
        )

    async def broadcast_counts(self, broadcast_id: str) -> dict:
        rows = await self._fetch_all(
            """
            SELECT status, COUNT(*) AS n FROM broadcast_recipients
            WHERE broadcast_id = ? GROUP BY status
            """,
            (broadcast_id,),
        )
        return _broadcast_counts_from_rows(rows)

    async def broadcast_ids(
        self,
        broadcast_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[int]:
        if status:
            rows = await self._fetch_all(
                """
                SELECT user_id FROM broadcast_recipients
                WHERE broadcast_id = ? AND status = ?
                ORDER BY user_id ASC LIMIT ? OFFSET ?
                """,
                (broadcast_id, status, max(1, int(limit)), max(0, int(offset))),
            )
        else:
            rows = await self._fetch_all(
                """
                SELECT user_id FROM broadcast_recipients
                WHERE broadcast_id = ?
                ORDER BY user_id ASC LIMIT ? OFFSET ?
                """,
                (broadcast_id, max(1, int(limit)), max(0, int(offset))),
            )
        return [int(row["user_id"]) for row in rows]

    async def broadcast_requeue_failed(self, broadcast_id: str) -> int:
        return await self._run_rowcount(
            """
            UPDATE broadcast_recipients SET status = 'PENDING', error = ''
            WHERE broadcast_id = ? AND status = 'FAILED'
            """,
            (broadcast_id,),
        )

    async def analytics_overview(self, since: str, until: str) -> dict:
        sql, params = analytics_overview_sql(since, until)
        return analytics_row(await self._fetch_one(self._q(sql), tuple(params)))

    async def analytics_series(
        self, since: str, until: str, bucket: str = "day"
    ) -> list[dict]:
        sql, params = analytics_series_sql(since, until, bucket)
        rows = await self._fetch_all(self._q(sql), tuple(params))
        return [
            {
                "bucket": str(row.get("grp") or ""),
                "messages": int(row.get("messages") or 0),
                "deleted": int(row.get("deleted") or 0),
                "edited": int(row.get("edited") or 0),
                "media": int(row.get("media") or 0),
            }
            for row in rows
        ]

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("SQLite closed")

    # -- raw helpers ----------------------------------------------------------
    # BARCHA so'rovlar shu yordamchilardan o'tadi — qayta urinish (retry +
    # backoff) va health yozuvi shu yerda, BITTA joyda (Postgres backend
    # bilan AYNAN bir xil xatti-harakat).

    async def _fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        async def run() -> list[dict]:
            cursor = await self.conn.execute(sql, params)
            rows = await cursor.fetchall()
            await cursor.close()
            return [dict(r) for r in rows]

        return await _resilient("fetch_all", run)

    async def _fetch_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        async def run() -> Optional[dict]:
            cursor = await self.conn.execute(sql, params)
            row = await cursor.fetchone()
            await cursor.close()
            return dict(row) if row else None

        return await _resilient("fetch_one", run)

    async def _fetchval(self, sql: str, params: tuple = ()) -> Any:
        async def run() -> Any:
            cursor = await self.conn.execute(sql, params)
            row = await cursor.fetchone()
            await cursor.close()
            return row[0] if row else None

        return await _resilient("fetchval", run)

    async def _execute(self, sql: str, params: tuple = ()) -> int:
        async def run() -> int:
            cursor = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cursor.lastrowid or 0

        return await _resilient("execute", run)

    async def _run_rowcount(self, sql: str, params: tuple = ()) -> int:
        """INSERT/UPDATE/DELETE dan keyin o'zgargan qatorlar soni."""
        async def run() -> int:
            cursor = await self.conn.execute(sql, params)
            await self.conn.commit()
            return int(cursor.rowcount or 0)

        return await _resilient("rowcount", run)

    async def ping(self) -> bool:
        """Ulanish tirikmi? (health ekrani uchun)"""
        value = await self._fetchval("SELECT 1")
        return value == 1

    def _q(self, sql: str) -> str:
        """Convert a $n-placeholder query to SQLite '?' style."""
        out = sql
        for i in range(20, 0, -1):
            out = out.replace(f"${i}", "?")
        return out

    async def _fo(self, sql: str, *params: Any) -> Optional[dict]:
        return await self._fetch_one(self._q(sql), tuple(params))

    # ======================================================================
    # USERS  (query bodies mirror app/database.py — keep them in sync)
    # ======================================================================

    async def upsert_user(
        self,
        user_id: int,
        username: Optional[str],
        first_name: Optional[str],
        last_name: Optional[str],
    ) -> None:
        now = now_iso()
        await self._execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name,
                               connected_at, last_activity, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name,
                last_name  = excluded.last_name,
                last_activity = excluded.last_activity
            """,
            (user_id, username, first_name, last_name, now, now, now),
        )

    async def touch_user(self, user_id: int) -> None:
        await self._execute(
            "UPDATE users SET last_activity = ? WHERE user_id = ?",
            (now_iso(), user_id),
        )

    async def get_user(self, user_id: int) -> Optional[dict]:
        return await self._fo("SELECT * FROM users WHERE user_id = $1", user_id)

    async def all_users(self) -> list[dict]:
        return await self._fetch_all("SELECT * FROM users ORDER BY created_at DESC")

    async def users_page(self, search: str = "", *, limit: int = 50, offset: int = 0) -> list[dict]:
        search = str(search or "").strip()
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        where = ""
        params: list[Any] = []
        if search:
            like = f"%{search}%"
            where = (
                "WHERE CAST(u.user_id AS TEXT) LIKE ? "
                "OR COALESCE(u.username,'') LIKE ? "
                "OR COALESCE(u.first_name,'') LIKE ? "
                "OR COALESCE(u.last_name,'') LIKE ?"
            )
            params.extend([like, like, like, like])
        params.extend([limit, offset])
        return await self._fetch_all(
            f"""
            SELECT u.*,
                   COUNT(c.business_connection_id) AS connections_count,
                   SUM(CASE WHEN c.is_enabled = 1 THEN 1 ELSE 0 END) AS active_connections
            FROM users u
            LEFT JOIN connections c ON c.user_id = u.user_id
            {where}
            GROUP BY u.user_id
            ORDER BY u.last_activity DESC, u.created_at DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params),
        )

    async def count_users_filtered(self, search: str = "") -> int:
        search = str(search or "").strip()
        if not search:
            return int(await self._fetchval("SELECT COUNT(*) FROM users") or 0)
        like = f"%{search}%"
        return int(await self._fetchval(
            """SELECT COUNT(*) FROM users u
               WHERE CAST(u.user_id AS TEXT) LIKE ?
                  OR COALESCE(u.username,'') LIKE ?
                  OR COALESCE(u.first_name,'') LIKE ?
                  OR COALESCE(u.last_name,'') LIKE ?""",
            (like, like, like, like),
        ) or 0)

    async def delete_user(self, user_id: int) -> bool:
        """Operational user data-ni atomar o'chiradi, audit tarixini saqlab qoladi."""
        user_id = int(user_id)
        cur = await self._conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        exists = await cur.fetchone()
        await cur.close()
        if not exists:
            return False
        try:
            await self._conn.execute("BEGIN")
            await self._conn.execute("DELETE FROM broadcast_recipients WHERE user_id = ?", (user_id,))
            await self._conn.execute("DELETE FROM events WHERE user_id = ?", (user_id,))
            await self._conn.execute("DELETE FROM connections WHERE user_id = ?", (user_id,))
            await self._conn.execute("UPDATE activity_log SET user_id = NULL WHERE user_id = ?", (user_id,))
            await self._conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        return True

    async def online_users(self, window_seconds: int = 120) -> list[dict]:
        threshold = (local_now() - timedelta(seconds=window_seconds)).isoformat(
            timespec="seconds"
        )
        return await self._fetch_all(
            """
            SELECT * FROM users
            WHERE last_activity >= ?
            ORDER BY last_activity DESC
            """,
            (threshold,),
        )

    async def count_users(self) -> int:
        row = await self._fetch_one("SELECT COUNT(*) AS n FROM users")
        return row["n"] if row else 0

    async def count_online(self, window_seconds: int = 120) -> int:
        threshold = (local_now() - timedelta(seconds=window_seconds)).isoformat(
            timespec="seconds"
        )
        row = await self._fetch_one(
            "SELECT COUNT(*) AS n FROM users WHERE last_activity >= ?",
            (threshold,),
        )
        return row["n"] if row else 0

    async def user_stats(self, user_id: int) -> dict:
        """Statistika ekranining barcha raqamlari — BITTA so'rov
        (Postgres backenddagisi bilan AYNAN bir xil natija)."""
        row = await self._fetch_one(
            """
            SELECT
              (SELECT COUNT(*) FROM users)                        AS users_total,
              (SELECT COUNT(*) FROM events WHERE user_id = ?)     AS events_total,
              (SELECT COUNT(*) FROM events
                 WHERE user_id = ? AND event_type = 'edit')       AS edits,
              (SELECT COUNT(*) FROM events
                 WHERE user_id = ? AND event_type = 'delete')     AS deletes,
              (SELECT COUNT(*) FROM events
                 WHERE user_id = ? AND event_type = 'delete_media') AS deletes_media,
              (SELECT COUNT(*) FROM connections
                 WHERE user_id = ? AND is_enabled = 1)            AS active_connections
            """,
            (user_id, user_id, user_id, user_id, user_id),
        )
        row = row or {}
        return {
            "users_total": int(row.get("users_total") or 0),
            "events_total": int(row.get("events_total") or 0),
            "edits": int(row.get("edits") or 0),
            "deletes": int(row.get("deletes") or 0),
            "deletes_media": int(row.get("deletes_media") or 0),
            "active_connections": int(row.get("active_connections") or 0),
        }

    async def has_active_connection(self, user_id: int) -> bool:
        """/start uchun: faol biznes-ulanish bormi (1 so'rov)."""
        value = await self._fetchval(
            "SELECT EXISTS(SELECT 1 FROM connections WHERE user_id = ? AND is_enabled = 1)",
            (user_id,),
        )
        return bool(value)

    # ======================================================================
    # EVENTS
    # ======================================================================

    async def add_event(
        self,
        user_id: int,
        event_type: str,
        details: str,
        chat_id: Optional[int] = None,
        chat_title: Optional[str] = None,
        message_id: Optional[int] = None,
        sender_id: Optional[int] = None,
        business_connection_id: Optional[str] = None,
    ) -> None:
        await self._execute(
            """
            INSERT INTO events (
                user_id, chat_id, chat_title, event_type, message_id,
                details, sender_id, business_connection_id, occurred_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, chat_id, chat_title, event_type, message_id, details,
                sender_id, business_connection_id, now_iso(),
            ),
        )

    async def recent_events(self, limit: int = 20, offset: int = 0) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM events ORDER BY id DESC LIMIT ? OFFSET ?",
            (max(1, int(limit)), max(0, int(offset))),
        )

    async def events_page(
        self,
        event_types: Optional[list[str]] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[dict]:
        """Turi bo'yicha filtrlab sahifalab o'qish (admin panel)."""
        if event_types:
            placeholders = ", ".join("?" for _ in event_types)
            return await self._fetch_all(
                f"""
                SELECT * FROM events WHERE event_type IN ({placeholders})
                ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (*event_types, max(1, int(limit)), max(0, int(offset))),
            )
        return await self.recent_events(limit, offset)

    async def count_events_multi(self, event_types: Optional[list[str]] = None) -> int:
        if event_types:
            placeholders = ", ".join("?" for _ in event_types)
            value = await self._fetchval(
                f"SELECT COUNT(*) FROM events WHERE event_type IN ({placeholders})",
                tuple(event_types),
            )
        else:
            value = await self._fetchval("SELECT COUNT(*) FROM events")
        return int(value or 0)

    async def count_events(
        self, event_type: Optional[str] = None, since: Optional[datetime] = None
    ) -> int:
        sql = "SELECT COUNT(*) AS n FROM events WHERE 1=1"
        params: list[Any] = []
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        if since:
            sql += " AND occurred_at >= ?"
            params.append(since.isoformat(timespec="seconds"))
        row = await self._fetch_one(sql, tuple(params))
        return row["n"] if row else 0

    async def count_user_events(self, user_id: int, event_type: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM events WHERE user_id = ?"
        params: list[Any] = [user_id]
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        row = await self._fetch_one(sql, tuple(params))
        return row["n"] if row else 0

    async def search_events(
        self,
        *,
        user_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        message_id: Optional[int] = None,
        connection_id: Optional[str] = None,
        event_type: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[dict]:
        clauses, params = _search_filters(
            user_id=user_id, chat_id=chat_id, message_id=message_id,
            connection_id=connection_id, event_type=event_type,
            since=since, until=until,
        )
        sql = "SELECT * FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        params.extend([max(1, int(limit)), max(0, int(offset))])
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        return await self._fetch_all(self._q(sql), tuple(params))

    async def count_search_events(self, **filters: Any) -> int:
        clauses, params = _search_filters(
            user_id=filters.get("user_id"),
            chat_id=filters.get("chat_id"),
            message_id=filters.get("message_id"),
            connection_id=filters.get("connection_id"),
            event_type=filters.get("event_type"),
            since=filters.get("since"),
            until=filters.get("until"),
        )
        sql = "SELECT COUNT(*) FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        value = await self._fetchval(self._q(sql), tuple(params))
        return int(value or 0)

    async def event_stats_since(self, since: Optional[datetime] = None) -> dict:
        """received / edited / deleted — BITTA so'rovda (analytics)."""
        sql = """
            SELECT
              SUM(CASE WHEN event_type NOT IN
                  ('edit','delete','delete_media','connection')
                  THEN 1 ELSE 0 END) AS received,
              SUM(CASE WHEN event_type = 'edit' THEN 1 ELSE 0 END) AS edited,
              SUM(CASE WHEN event_type IN ('delete','delete_media')
                  THEN 1 ELSE 0 END) AS deleted
            FROM events
        """
        if since is None:
            row = await self._fetch_one(sql)
        else:
            row = await self._fetch_one(
                sql + " WHERE occurred_at >= ?",
                (since.isoformat(timespec="seconds"),),
            )
        row = row or {}
        return {
            "received": int(row.get("received") or 0),
            "edited": int(row.get("edited") or 0),
            "deleted": int(row.get("deleted") or 0),
        }

    async def get_event_by_message(
        self, chat_id: int, message_id: int,
        business_connection_id: Optional[str] = None,
    ) -> Optional[dict]:
        if business_connection_id:
            return await self._fetch_one(
                """
                SELECT * FROM events
                WHERE chat_id = ? AND message_id = ?
                  AND (
                    business_connection_id = ?
                    OR (
                      business_connection_id IS NULL
                      AND user_id IN (
                        SELECT user_id FROM connections
                        WHERE business_connection_id = ?
                      )
                    )
                  )
                ORDER BY CASE WHEN business_connection_id = ? THEN 1 ELSE 0 END DESC, id DESC
                LIMIT 1
                """,
                (
                    chat_id, message_id, business_connection_id,
                    business_connection_id, business_connection_id,
                ),
            )
        return await self._fo(
            """
            SELECT * FROM events
            WHERE chat_id = $1 AND message_id = $2
            ORDER BY id DESC LIMIT 1
            """,
            chat_id,
            message_id,
        )

    async def update_event_details(
        self, chat_id: int, message_id: int, details: str,
        business_connection_id: Optional[str] = None,
    ) -> bool:
        """Shu xabarning ENG OXIRGI mos yozuvini yangilaydi."""
        if business_connection_id:
            changed = await self._run_rowcount(
                """
                UPDATE events SET details = ?
                WHERE id = (
                    SELECT id FROM events
                    WHERE chat_id = ? AND message_id = ?
                      AND (
                        business_connection_id = ?
                        OR (
                          business_connection_id IS NULL
                          AND user_id IN (
                            SELECT user_id FROM connections
                            WHERE business_connection_id = ?
                          )
                        )
                      )
                    ORDER BY CASE WHEN business_connection_id = ? THEN 1 ELSE 0 END DESC, id DESC
                    LIMIT 1
                )
                """,
                (
                    details, chat_id, message_id, business_connection_id,
                    business_connection_id, business_connection_id,
                ),
            )
        else:
            changed = await self._run_rowcount(
                """
                UPDATE events SET details = ?
                WHERE id = (
                    SELECT id FROM events
                    WHERE chat_id = ? AND message_id = ?
                    ORDER BY id DESC LIMIT 1
                )
                """,
                (details, chat_id, message_id),
            )
        return changed > 0

    async def prune_events(self, keep: int = 20_000) -> None:
        await self._execute(
            """
            DELETE FROM events WHERE id NOT IN (
                SELECT id FROM events ORDER BY id DESC LIMIT ?
            )
            """,
            (keep,),
        )

    async def prune_events_older_than(self, days: int) -> int:
        """Retention: ``days`` kundan eski hodisalarni o'chiradi."""
        cutoff = (local_now() - timedelta(days=max(1, int(days)))).isoformat(
            timespec="seconds"
        )
        return await self._run_rowcount(
            "DELETE FROM events WHERE occurred_at < ?", (cutoff,)
        )

    # ======================================================================
    # CONNECTIONS
    # ======================================================================

    async def upsert_connection(
        self,
        business_connection_id: str,
        user_id: int,
        is_enabled: bool,
        user_chat_id: Optional[int] = None,
    ) -> None:
        now = now_iso()
        existing = await self._fo(
            "SELECT * FROM connections WHERE business_connection_id = $1",
            business_connection_id,
        )
        if existing is None:
            await self._execute(
                """
                INSERT INTO connections
                    (business_connection_id, user_id, user_chat_id, is_enabled, connected_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (business_connection_id, user_id, user_chat_id, 1 if is_enabled else 0, now),
            )
        else:
            connected_at = existing.get("connected_at")
            disconnected_at = existing.get("disconnected_at")
            if is_enabled:
                disconnected_at = None
                connected_at = connected_at or now
            else:
                disconnected_at = now
            await self._execute(
                """
                UPDATE connections
                SET is_enabled = ?, connected_at = ?, disconnected_at = ?,
                    user_chat_id = COALESCE(?, user_chat_id)
                WHERE business_connection_id = ?
                """,
                (1 if is_enabled else 0, connected_at, disconnected_at, user_chat_id,
                 business_connection_id),
            )

    async def get_connection(self, business_connection_id: str) -> Optional[dict]:
        return await self._fo(
            "SELECT * FROM connections WHERE business_connection_id = $1",
            business_connection_id,
        )

    async def connected_user_ids(self) -> list[int]:
        rows = await self._fetch_all(
            "SELECT DISTINCT user_id FROM connections WHERE is_enabled = 1"
        )
        return [row["user_id"] for row in rows]

    async def connections_for_user(self, user_id: int) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM connections WHERE user_id = ? ORDER BY connected_at DESC",
            (user_id,),
        )

    # ======================================================================
    # INSTANCE LOCK — bir vaqtda faqat BITTA nusxa polling qilishi uchun
    # (Postgres versiyasi bilan AYNAN bir xil xatti-harakat)
    # ======================================================================

    async def claim_instance_lock(
        self,
        instance: str,
        host: str,
        pid: int,
        stale_seconds: float,
        service: Optional[str] = None,
        deployment: Optional[str] = None,
    ) -> Optional[dict]:
        """Qulfni ATOMIK olish; ``None`` — endi bizda, aks holda egasi.

        Postgres versiyasi bilan AYNAN bir xil: boshqariladigan deploy
        (``service`` berilgan) SHU servisning eski qulfini (hatto
        heartbeat hali "yangi" bo'lsa ham) egallaydi.
        """
        now = now_iso()
        stale_before = (
            local_now() - timedelta(seconds=stale_seconds)
        ).isoformat(timespec="seconds")
        owned = await self._fetchval(
            self._q(
                """
            INSERT INTO instance_lock
                (id, instance, host, pid, service, deployment,
                 started_at, heartbeat_at)
            VALUES ('bot', $1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (id) DO UPDATE SET
                instance     = excluded.instance,
                host         = excluded.host,
                pid          = excluded.pid,
                service      = excluded.service,
                deployment   = excluded.deployment,
                started_at   = excluded.started_at,
                heartbeat_at = excluded.heartbeat_at
            WHERE instance_lock.instance = excluded.instance
               OR instance_lock.heartbeat_at <= $8
               OR (
                    excluded.service IS NOT NULL
                    AND (instance_lock.service IS NULL
                         OR instance_lock.service = excluded.service)
                  )
            RETURNING instance
            """
            ),
            (instance, host, pid, service, deployment, now, now, stale_before),
        )
        if owned:
            return None
        return await self._fetch_one(
            "SELECT * FROM instance_lock WHERE id = 'bot'"
        )

    async def heartbeat_instance_lock(self, instance: str) -> bool:
        value = await self._fetchval(
            self._q(
                """
            UPDATE instance_lock SET heartbeat_at = $1
            WHERE id = 'bot' AND instance = $2
            RETURNING 1
            """
            ),
            (now_iso(), instance),
        )
        return bool(value)

    async def release_instance_lock(self, instance: str) -> None:
        await self._execute(
            "DELETE FROM instance_lock WHERE id = 'bot' AND instance = ?",
            (instance,),
        )
