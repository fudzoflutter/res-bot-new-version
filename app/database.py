"""
Saqlash qatlami – AVTOMATIK backend tanlovi.

* ``.env`` da SUPABASE_DB_URL bor bo'lsa  -> Supabase Postgres (asyncpg)
* yo'q bo'lsa                             -> mahalliy SQLite (zaxira)

Ikkala backend bir xil API beradi (app/storage_sqlite.py bilan solishtiring):
handlerlar qaysi backend ishlayotganini sezmaydi.

Supabase ulanishi (bir marta):
1. Supabase Dashboard -> loyihangiz -> yashil "Connect" tugmasi ->
   "Connection pooling" -> URI ni nusxalang:
   postgresql://postgres.xxxx:PAROL@aws-0-region.pooler.supabase.com:6543/postgres
2. .env ga yozing: SUPABASE_DB_URL=...
3. Botni qayta ishga tushiring — jadvallar avtomatik yaratiladi va mavjud
   bot.db ma'lumotlari BIR MARTA import qilinadi (supabase_migrations da
   belgilanadi).

Bot jadvallari: ``users``, ``events`` (xabarlar keshi — o'chirilgan
xabarlarni qayta yuborish uchun), ``connections``, ``activity_log``,
``bot_settings`` va ``instance_lock``.  Foydalanuvchi kirish tizimi
(ruxsat / rad / ban) butunlay OLIB TASHLANGAN — admin panel faqat
server-side admin tekshiruvi bilan himoyalanadi.
"""

from __future__ import annotations

from app.utils.timeutils import local_now

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from app.config import settings
from app.services import db_health

logger = logging.getLogger(__name__)

def _search_filters(
    *,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    message_id: Optional[int] = None,
    connection_id: Optional[str] = None,
    event_type: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> tuple[list[str], list[Any]]:
    """Qidiruv filtrlari (ikkala backend bir xil shartlarni ishlatadi)."""
    clauses: list[str] = []
    params: list[Any] = []

    def add(clause: str, value: Any) -> None:
        params.append(value)
        clauses.append(clause.format(n=len(params)))

    if user_id is not None:
        add("user_id = ${n}", int(user_id))
    if chat_id is not None:
        add("chat_id = ${n}", int(chat_id))
    if message_id is not None:
        add("message_id = ${n}", int(message_id))
    if connection_id:
        # Yangi yozuvlar connection_id bilan izolyatsiya qilinadi. Migratsiyadan
        # OLDIN yozilgan eventlarda bu ustun NULL bo'lishi mumkin; ularni faqat
        # shu connection egasining user_id si mos tushsa legacy fallback sifatida
        # ko'rsatamiz. Ikki parametr ataylab alohida — SQLite $n -> ? konvertori
        # bir placeholder takrorlansa ham alohida binding talab qiladi.
        params.extend([str(connection_id), str(connection_id)])
        n1, n2 = len(params) - 1, len(params)
        clauses.append(
            "(business_connection_id = ${n1} OR "
            "(business_connection_id IS NULL AND user_id IN "
            "(SELECT user_id FROM connections WHERE business_connection_id = ${n2})))"
            .format(n1=n1, n2=n2)
        )
    if event_type:
        add("event_type = ${n}", str(event_type))
    if since:
        add("occurred_at >= ${n}", since.isoformat(timespec="seconds"))
    if until:
        add("occurred_at <= ${n}", until.isoformat(timespec="seconds"))
    return clauses, params


# ---------------------------------------------------------------------------
# ANALYTICS — umumiy SQL quruvchilar (ikkala backend AYNAN shu SQL ni
# ishlatadi; SQLite backend `_q()` bilan $n -> ? ga o'giradi).
#
# FILTR: barcha so'rovlar PARAMETRLANGAN ($n) — hech qanday qiymat SQL
# matniga qo'shilmaydi (injection yo'q).  Vaqt maydonlari TEXT ISO-8601,
# shuning uchun leksikografik taqqoslash to'g'ri ishlaydi (indeks ishlaydi).
# ---------------------------------------------------------------------------

#: Hodisa turlari (DB qiymatlari) — hisobotlar bilan AYNAN bir xil.
ANALYTICS_MEDIA_EVENTS = (
    "sticker", "photo", "video", "animation",
    "voice", "video_note", "audio", "document",
)
ANALYTICS_EXCLUDED_EVENTS = ("edit", "delete", "delete_media", "connection")


def _sql_list(values: tuple[str, ...]) -> str:
    """``('a','b')`` -> ``'a','b'`` (faqat kod ichidagi doimiy ro'yxatlar)."""
    return ", ".join(f"'{value}'" for value in values)


def analytics_overview_sql(since: str, until: str) -> tuple[str, list[Any]]:
    """Dashboard ko'rsatkichlari — BITTA so'rov (N+1 yo'q).

    Qaytaradi: (sql, params).  ``since``/``until`` — ISO satrlar.
    """
    params: list[Any] = []

    def ph(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    def event_window() -> str:
        return f" AND occurred_at >= {ph(since)} AND occurred_at <= {ph(until)}"

    media = _sql_list(ANALYTICS_MEDIA_EVENTS)
    excluded = _sql_list(ANALYTICS_EXCLUDED_EVENTS)
    sql = f"""
        SELECT
          (SELECT COUNT(*) FROM users) AS users_total,
          (SELECT COUNT(*) FROM users
             WHERE (last_activity >= {ph(since)}
                    AND last_activity <= {ph(until)})
                OR user_id IN (SELECT user_id FROM events
                               WHERE occurred_at >= {ph(since)}
                                 AND occurred_at <= {ph(until)})) AS users_active,
          (SELECT COUNT(*) FROM connections) AS connections_total,
          (SELECT COUNT(*) FROM connections
             WHERE is_enabled = {ph(True)}) AS connections_active,
          (SELECT COUNT(*) FROM events
             WHERE 1=1{event_window()}
               AND event_type NOT IN ({excluded})) AS messages,
          (SELECT COUNT(*) FROM events
             WHERE 1=1{event_window()}
               AND event_type IN ('delete','delete_media')) AS deleted,
          (SELECT COUNT(*) FROM events
             WHERE 1=1{event_window()}
               AND event_type = 'edit') AS edited,
          (SELECT COUNT(*) FROM events
             WHERE 1=1{event_window()}
               AND event_type IN ({media})) AS media
    """
    return sql, params


def analytics_series_sql(since: str, until: str, bucket: str) -> tuple[str, list[Any]]:
    """Vaqt kesimidagi seriya (bucket: ``hour`` yoki ``day``).

    Guruhlash ``occurred_at`` (ISO-8601 TEXT) kesimi orqali qilinadi:

    * ``hour`` -> ``substr(occurred_at, 1, 13)`` = ``YYYY-MM-DDTHH``
    * ``day``  -> ``substr(occurred_at, 1, 10)`` = ``YYYY-MM-DD``

    Shu sababli SQL SQLite va Postgres uchun BIR XIL bo'lib qoladi.
    ``bucket`` faqat kod ichida tanlanadi (foydalanuvchi matni emas).
    """
    width = 13 if bucket == "hour" else 10
    media = _sql_list(ANALYTICS_MEDIA_EVENTS)
    excluded = _sql_list(ANALYTICS_EXCLUDED_EVENTS)
    sql = f"""
        SELECT substr(occurred_at, 1, {width}) AS grp,
               SUM(CASE WHEN event_type NOT IN ({excluded})
                        THEN 1 ELSE 0 END)                       AS messages,
               SUM(CASE WHEN event_type IN ('delete','delete_media')
                        THEN 1 ELSE 0 END)                       AS deleted,
               SUM(CASE WHEN event_type = 'edit'
                        THEN 1 ELSE 0 END)                       AS edited,
               SUM(CASE WHEN event_type IN ({media})
                        THEN 1 ELSE 0 END)                       AS media
        FROM events
        WHERE occurred_at >= $1 AND occurred_at <= $2
        GROUP BY grp
        ORDER BY grp ASC
    """
    return sql, [since, until]


def analytics_row(row: Optional[dict]) -> dict[str, int]:
    """Xom natijani butun sonli ko'rsatkichlarga aylantiradi."""
    data = row or {}
    keys = (
        "users_total", "users_active", "connections_total", "connections_active",
        "messages", "deleted", "edited", "media",
    )
    return {key: int(data.get(key) or 0) for key in keys}


# ---------------------------------------------------------------------------
# BROADCAST holatlari. UNKNOWN ataylab retry qilinmaydi: Telegram so'rovi
# serverga yetib borgan bo'lishi mumkin, shuning uchun qayta yuborish dublikat
# keltirib chiqarishi ehtimoli bor. UNKNOWN faqat operator ko'rib chiqishi uchun.
# ---------------------------------------------------------------------------
BROADCAST_PENDING = "PENDING"
BROADCAST_SENDING = "SENDING"
BROADCAST_SENT = "SENT"
BROADCAST_FAILED = "FAILED"
BROADCAST_UNKNOWN = "UNKNOWN"
BROADCAST_STATUSES = (
    BROADCAST_PENDING, BROADCAST_SENDING, BROADCAST_SENT, BROADCAST_FAILED,
    BROADCAST_UNKNOWN,
)


def _broadcast_counts_from_rows(rows: Any) -> dict[str, int]:
    """GROUP BY natijasini aniq broadcast holatlariga aylantiradi."""
    result = {"pending": 0, "sending": 0, "sent": 0, "failed": 0, "unknown": 0}
    for row in rows or ():
        status = str(row.get("status") or "").strip().lower()
        if status in result:
            result[status] = int(row.get("n") or 0)
    result["total"] = sum(result.values())
    return result


# ---------------------------------------------------------------------------
# Schema – Postgres version of the old SQLite schema.
# "IF NOT EXISTS" makes it safe to run on every startup.
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        BIGINT PRIMARY KEY,
    username       TEXT,
    first_name     TEXT,
    last_name      TEXT,
    connected_at   TEXT,
    last_activity  TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    sender_id    BIGINT,
    business_connection_id TEXT,
    chat_id      BIGINT,
    chat_title   TEXT,
    event_type   TEXT NOT NULL,
    message_id   BIGINT,
    details      TEXT NOT NULL DEFAULT \'\',
    occurred_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
    business_connection_id TEXT PRIMARY KEY,
    user_id                BIGINT NOT NULL,
    user_chat_id           BIGINT,
    is_enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    connected_at           TEXT,
    disconnected_at        TEXT
);

CREATE TABLE IF NOT EXISTS instance_lock (
    id           TEXT PRIMARY KEY,
    instance     TEXT NOT NULL,
    host         TEXT,
    pid          BIGINT,
    service      TEXT,
    deployment   TEXT,
    started_at   TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

-- Activity log: admin-visible timeline of system events.
CREATE TABLE IF NOT EXISTS activity_log (
    id              BIGSERIAL PRIMARY KEY,
    event_type      TEXT NOT NULL,
    connection_id   TEXT,
    user_id         BIGINT,
    description     TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT \'INFO\',
    occurred_at     TEXT NOT NULL
);

-- Bot settings: key-value store for admin-configurable settings.
CREATE TABLE IF NOT EXISTS bot_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_time         ON events (occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_type         ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_user         ON events (user_id);
CREATE INDEX IF NOT EXISTS idx_events_chat_message ON events (chat_id, message_id);
CREATE INDEX IF NOT EXISTS idx_events_type_time    ON events (event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_user_time    ON events (user_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_activity_log_conn   ON activity_log (connection_id);
CREATE INDEX IF NOT EXISTS idx_activity_log_time   ON activity_log (occurred_at);
CREATE INDEX IF NOT EXISTS idx_activity_log_type   ON activity_log (event_type);
CREATE INDEX IF NOT EXISTS idx_connections_user    ON connections (user_id);

-- One-time import marker (so we never import the same SQLite file twice).
CREATE TABLE IF NOT EXISTS supabase_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Broadcast (reklama) progressi: har bir oluvchi uchun ALOHIDA holat.
-- Jarayon o'lsa ham progress yo'qolmaydi. UNKNOWN holati Telegram qabul qilgan-
-- qabul qilmaganini aniq bilib bo'lmagan holat uchun; u Retry Failed'ga kiritilmaydi.
CREATE TABLE IF NOT EXISTS broadcast_recipients (
    broadcast_id TEXT NOT NULL,
    user_id      BIGINT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'PENDING',
    error        TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (broadcast_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_status
    ON broadcast_recipients (broadcast_id, status);
"""


def _now() -> str:
    """Current local time as ISO string (same as the SQLite version)."""
    return local_now().isoformat(timespec="seconds")


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp stored in the DB (None-safe)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _b(value: Any) -> bool:
    """Normalize ints/bools coming from different drivers to bool."""
    return bool(value)


# Severity darajalari (log filtri uchun).
SEVERITIES = ("INFO", "WARNING", "ERROR", "CRITICAL")


def _severity_levels(min_severity: str) -> list[str]:
    """``min_severity`` darajasidan yuqori bo'lganlarni qaytaradi."""
    value = str(min_severity or "").upper()
    if value not in SEVERITIES:
        return list(SEVERITIES)
    return list(SEVERITIES[SEVERITIES.index(value):])


# --- ISHONCHLILIK (reliability) -------------------------------------------
# Vaqtinchalik (transient) xatolar — ulanish uzildi, timeout, band baza —
# EKSPONENSIAL BACKOFF bilan qayta uriniladi.  Doimiy xatolar (noto'g'ri
# SQL, UNIQUE buzilishi) qayta urinilmaydi va JIM YUTILMAYDI: logga to'liq
# traceback bilan yoziladi va yuqoriga ko'tariladi.
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.5   # 0.5s -> 1s -> (2s)
RETRY_MAX_DELAY = 5.0


def _transient(exc: BaseException) -> bool:
    """Xato VAQTINCHALIKMI (qayta urinish foydali bo'ladimi)?"""
    import asyncio
    import sqlite3

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, OSError)):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "locked", "busy", "unable to open", "disk i/o", "interrupted",
                "database connection is closed",
            )
        )
    try:  # asyncpg faqat Postgres backendda o'rnatilgan bo'lishi mumkin
        import asyncpg
    except ModuleNotFoundError:  # pragma: no cover – SQLite-only muhit
        return False
    for name in (
        "PostgresConnectionError",
        "InterfaceError",
        "TooManyConnectionsError",
        "InternalServerError",
        "CannotConnectNowError",
        "ConnectionDoesNotExistError",
        "AdminShutdownError",
    ):
        cls = getattr(asyncpg, name, None)
        if cls is not None and isinstance(exc, cls):
            return True
    return False


# Nested alert himoyasi: alert yuborish ichida yana DB xatosi bo'lsa
# (masalan activity log yozishda) cheksiz zanjir bo'lmasligi uchun.
_alerts_in_progress = False


async def _alert_db_error(
    op: str, exc: BaseException, attempt: int, attempts: int
) -> None:
    """DB xatosi bo'yicha adminga alert yuboradi (cooldown bilan).

    Alert matnida FAQAT sanitizatsiya qilingan sabab bo'ladi — parol yoki
    DSN hech qachon ko'rinmaydi.
    """
    global _alerts_in_progress
    if _alerts_in_progress:
        return
    _alerts_in_progress = True
    try:
        from app.services import alerts as alert_service

        await alert_service.alerts.notify(
            f"db_error:{op}",
            "DATABASE ERROR",
            "Database so'rovi bajarilmadi.\n"
            f"Op: {op}\n"
            f"Sabab: {db_health.sanitize(str(exc))}\n"
            f"Retry: {attempt}/{attempts}\n"
            f"Backend: {db_health.health.backend}",
            severity=alert_service.SEVERITY_ERROR,
            also_log=True,
        )
    except Exception:  # noqa: BLE001 – alert xatosi asosiy xatoni yashirmasin
        logger.debug("DB alert yuborilmadi", exc_info=True)
    finally:
        _alerts_in_progress = False


async def _resilient(op: str, factory: Any, *, attempts: int = RETRY_ATTEMPTS,
                     base: float = RETRY_BASE_DELAY) -> Any:
    """DB so'rovini qayta urinishlar bilan bajaradi (natija yoki xato).

    Har bir urinish natijasi :mod:`app.services.db_health` ga yoziladi —
    admin panel "🗄 Database" va "🤖 Bot Health" ekranlari shu yerdan
    o'qiydi.  Parol/kalit logga tushmasligi uchun matn avval sanitize
    qilinadi (``db_health.sanitize``).
    """
    import asyncio

    attempts = max(1, int(attempts))
    last: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            result = await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 – qaror quyida
            last = exc
            db_health.health.record_error(exc, op=op)
            if attempt >= attempts or not _transient(exc):
                db_health.health.record_failure(exc, op=op)
                logger.error(
                    "DB so'rovi bajarilmadi (%s, urinish %d/%d): %s",
                    op, attempt, attempts, db_health.sanitize(str(exc)),
                    exc_info=exc,
                )
                # TALAB: DB xatosi LOG qilinadi + admin panel health'da
                # ko'rinadi (db_health) + adminga ALERT yuboriladi.  Alert
                # cooldown bilan ishlaydi (spam bo'lmasligi uchun) va
                # activity log imkon bo'lsa yoziladi (DB o'zi o'lgan bo'lsa
                # u ham yozilmaydi — bu normal).
                await _alert_db_error(op, exc, attempt, attempts)
                raise
            db_health.health.record_retry()
            delay = min(RETRY_MAX_DELAY, base * (2 ** (attempt - 1)))
            logger.warning(
                "DB vaqtincha xatosi (%s) — %.1fs dan keyin qayta urinish %d/%d: %s",
                op, delay, attempt + 1, attempts, db_health.sanitize(str(exc)),
            )
            await asyncio.sleep(delay)
        else:
            db_health.health.record_success(op)
            return result
    raise last  # pragma: no cover – yuqoridagi raise yetib boradi


class Database:
    """Fasad: real ishlarni Postgres yoki SQLite backend bajaradi.

    ``init()`` avtomatik tanlaydi:
    * SUPABASE_DB_URL berilgan bo'lsa  -> Postgres klassi (quyida)
    * aks holda                        -> app/storage_sqlite.SqliteDatabase
    """

    def __init__(self) -> None:
        self._backend: Any = None  # PostgresDatabase | SqliteDatabase
        # init() paytida Supabase ishlamay qolsa — sabab shu yerda qoladi;
        # main.py bot yaratilgach adminga ANIQ alert yuboradi.
        self.startup_error: str = ""
        self.startup_backend: str = db_health.BACKEND_NONE

    # -- lifecycle ---------------------------------------------------------

    async def init(self) -> None:
        """Backendni tanlaydi va ishga tushiradi.

        Supabase vaqtincha ishlamasa:
          1. so'rov bir necha marta qayta uriniladi (eksponensial backoff);
          2. baribir ulanmasa — LOCAL SQLite fallback ishga tushadi va bot
             ISHLASHDA DAVOM ETADI (to'liq o'lim yo'q);
          3. holat "degraded" deb belgilanadi: log (CRITICAL), admin panelda
             🔴 va bot ishga tushgach adminga alert yuboriladi.

        Supabase'ga QAYTISH: avtomatik EMAS — qayta ishga tushirish (restart)
        kerak.  Restartda Supabase ulansa, fallback paytida SQLite'ga yozilgan
        yozuvlar kursorli, idempotent tarzda AVTOMATIK ko'chiriladi
        (``app.services.fallback_sync``); users/connections/settings faqat
        SQLite yozuvi PRIMARY'dagidan YANGIROQ bo'lsagina yangilanadi.
        Fallback holatini admin panelda "SQLite fallback: Active" deb ko'rasiz.
        """
        if settings.supabase_db_url:
            db_health.health.set_backend(db_health.BACKEND_POSTGRES)
            primary = PostgresDatabase()
            try:
                await _resilient("init_postgres", primary.init, attempts=3, base=1.0)
            except Exception as exc:  # noqa: BLE001 – fallback qarori
                self.startup_error = db_health.sanitize(str(exc))
                logger.critical(
                    "🔴 Supabase ishga tushmadi (%s) — SQLite fallbackga o'tilmoqda: %s",
                    settings.supabase_host, db_health.sanitize(str(exc)),
                )
                try:
                    await primary.close()
                except Exception:  # noqa: BLE001
                    pass
                db_health.health.mark_down(self.startup_error)
                await self._init_sqlite(fallback=True)
                return
            self._backend = primary
            self.startup_backend = db_health.BACKEND_POSTGRES
            return

        await self._init_sqlite(fallback=False)

    async def _init_sqlite(self, *, fallback: bool) -> None:
        """Mahalliy SQLite backendni ishga tushiradi (zaxira yoki asosiy)."""
        from app.storage_sqlite import SqliteDatabase

        backend = SqliteDatabase()
        await backend.init()
        self._backend = backend
        self.startup_backend = db_health.BACKEND_SQLITE
        db_health.health.set_backend(db_health.BACKEND_SQLITE, fallback=fallback)
        if fallback:
            logger.critical(
                "⚠️ DEGRADED REJIM: bot SQLite (%s) ustida ishlayapti. "
                "Supabase qaytganda QAYTA ISHGA TUSHIRING.",
                settings.db_path,
            )
        else:
            logger.warning(
                "SUPABASE_DB_URL yo'q — hozircha mahalliy SQLite (%s) ishlatiladi. "
                "Supabasega o'tish uchun .env ga SUPABASE_DB_URL qo'shing.",
                settings.db_path,
            )

    @property
    def backend_name(self) -> str:
        """Joriy backend nomi (admin panel uchun)."""
        return db_health.health.backend

    @property
    def using_fallback(self) -> bool:
        """True — SQLite fallback ishlayapti (Supabase ishlamayapti)."""
        return db_health.health.fallback_active

    async def ping(self) -> bool:
        """Yengil tekshiruv: baza javob beryaptimi? (health ekrani uchun)."""
        if self._backend is None:
            return False
        try:
            value = await self._backend.ping()
        except Exception as exc:  # noqa: BLE001 – ping hech qachon ko'tarmaydi
            db_health.health.record_failure(exc, op="ping")
            return False
        db_health.health.record_success("ping")
        return bool(value)

    async def close(self) -> None:
        if self._backend is not None:
            await self._backend.close()
            self._backend = None

    # -- delegation --------------------------------------------------------
    # Har bir metod real backendga yo'naltiriladi.

    async def upsert_user(self, user_id: int, username, first_name, last_name) -> None:
        await self._backend.upsert_user(user_id, username, first_name, last_name)

    async def touch_user(self, user_id: int) -> None:
        await self._backend.touch_user(user_id)

    async def get_user(self, user_id: int) -> Optional[dict]:
        return await self._backend.get_user(user_id)

    async def all_users(self) -> list[dict]:
        return await self._backend.all_users()

    async def users_page(self, search: str = "", *, limit: int = 50, offset: int = 0) -> list[dict]:
        return await self._backend.users_page(search, limit=limit, offset=offset)

    async def count_users_filtered(self, search: str = "") -> int:
        return await self._backend.count_users_filtered(search)

    async def delete_user(self, user_id: int) -> bool:
        return await self._backend.delete_user(user_id)

    async def online_users(self, window_seconds: int = 120) -> list[dict]:
        return await self._backend.online_users(window_seconds)

    async def count_users(self) -> int:
        return await self._backend.count_users()

    async def count_online(self, window_seconds: int = 120) -> int:
        return await self._backend.count_online(window_seconds)

    async def user_stats(self, user_id: int) -> dict:
        """Statistika ekrani uchun BITTA so'rovdagi barcha hisoblar."""
        return await self._backend.user_stats(user_id)

    async def has_active_connection(self, user_id: int) -> bool:
        """Foydalanuvchining FAOL biznes-ulanishi bormi?"""
        return await self._backend.has_active_connection(user_id)

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
        await self._backend.add_event(
            user_id, event_type, details, chat_id, chat_title, message_id,
            sender_id, business_connection_id
        )

    async def recent_events(self, limit: int = 20, offset: int = 0) -> list[dict]:
        return await self._backend.recent_events(limit, offset)

    async def events_page(
        self,
        event_types: Optional[list[str]] = None,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> list[dict]:
        """Turi bo'yicha filtrlab sahifalab o'qish (admin panel ro'yxatlari)."""
        return await self._backend.events_page(event_types, limit, offset)

    async def count_events_multi(self, event_types: Optional[list[str]] = None) -> int:
        """Ro'yxat bo'yicha jami yozuvlar soni (sahifalash uchun)."""
        return await self._backend.count_events_multi(event_types)

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
        """Global qidiruv (user / chat / message / connection / type / sana)."""
        return await self._backend.search_events(
            user_id=user_id, chat_id=chat_id, message_id=message_id,
            connection_id=connection_id, event_type=event_type,
            since=since, until=until, limit=limit, offset=offset,
        )

    async def count_search_events(self, **filters: Any) -> int:
        """Qidiruv natijalari soni (sahifalash uchun)."""
        return await self._backend.count_search_events(**filters)

    async def event_stats_since(self, since: Optional[datetime] = None) -> dict:
        """BITTA so'rovda received / edited / deleted (analytics uchun)."""
        return await self._backend.event_stats_since(since)

    async def count_events(
        self, event_type: Optional[str] = None, since: Optional[datetime] = None
    ) -> int:
        return await self._backend.count_events(event_type, since)

    async def count_user_events(self, user_id: int, event_type: Optional[str] = None) -> int:
        return await self._backend.count_user_events(user_id, event_type)

    async def get_event_by_message(
        self, chat_id: int, message_id: int,
        business_connection_id: Optional[str] = None,
    ) -> Optional[dict]:
        return await self._backend.get_event_by_message(
            chat_id, message_id, business_connection_id
        )

    async def update_event_details(
        self, chat_id: int, message_id: int, details: str,
        business_connection_id: Optional[str] = None,
    ) -> bool:
        """Shu xabar yozuvining tarkibini yangilaydi (tahrirlash holati).

        True — yozuv topildi va yangilandi. Tahrirlashda chaqiriladi:
        xabar keyin o'chirilsa, hisobotda ENG OXIRGI tarkib ko'rsatiladi.
        """
        return await self._backend.update_event_details(
            chat_id, message_id, details, business_connection_id
        )

    async def prune_events(self, keep: int = 20_000) -> None:
        await self._backend.prune_events(keep)

    async def upsert_connection(
        self,
        business_connection_id: str,
        user_id: int,
        is_enabled: bool,
        user_chat_id: Optional[int] = None,
    ) -> None:
        await self._backend.upsert_connection(
            business_connection_id, user_id, is_enabled, user_chat_id
        )

    async def get_connection(self, business_connection_id: str) -> Optional[dict]:
        return await self._backend.get_connection(business_connection_id)

    async def connected_user_ids(self) -> list[int]:
        return await self._backend.connected_user_ids()

    async def connections_for_user(self, user_id: int) -> list[dict]:
        return await self._backend.connections_for_user(user_id)

    # -- instance lock (bir vaqtda faqat BITTA nusxa polling qiladi) --------

    async def claim_instance_lock(
        self,
        instance: str,
        host: str,
        pid: int,
        stale_seconds: float,
        service: Optional[str] = None,
        deployment: Optional[str] = None,
    ) -> Optional[dict]:
        """Qulfni olishga urinadi.

        ``None`` — qulf endi BIZDA (polling boshlash mumkin).  Aks holda
        hozirgi egasi haqidagi yozuv qaytadi (boshqa nusxa tirik).
        """
        return await self._backend.claim_instance_lock(
            instance, host, pid, stale_seconds, service, deployment
        )

    async def heartbeat_instance_lock(self, instance: str) -> bool:
        """Qulf hali bizda ekanini tasdiqlaydi (heartbeat yangilanadi)."""
        return await self._backend.heartbeat_instance_lock(instance)

    async def release_instance_lock(self, instance: str) -> None:
        """Chiqishda qulfni bo'shatadi (keyingi start darhol ishga tushadi)."""
        await self._backend.release_instance_lock(instance)

    # -- activity log -------------------------------------------------------

    async def add_activity_log(
        self,
        event_type: str,
        description: str,
        *,
        connection_id: Optional[str] = None,
        user_id: Optional[int] = None,
        severity: str = "INFO",
    ) -> None:
        """Faoliyat jurnalga yozadi (admin panel Activity Log uchun)."""
        await self._backend.add_activity_log(
            event_type, description,
            connection_id=connection_id, user_id=user_id, severity=severity,
        )

    async def recent_activity_logs(self, limit: int = 50) -> list[dict]:
        """Eng yangi faoliyat yozuvlari."""
        return await self._backend.recent_activity_logs(limit)

    async def count_activity_logs(self) -> int:
        return await self._backend.count_activity_logs()

    # -- bot settings -------------------------------------------------------

    async def get_setting(self, key: str, default: str = "") -> str:
        """Sozlamani olish (topilmasa ``default``)."""
        return await self._backend.get_setting(key, default)

    async def set_setting(self, key: str, value: str) -> None:
        """Sozlamani saqlash.

        ``maintenance_mode`` yozilganda hold-mode keshi darhol bekor qilinadi
        (aks holda admin yoqgan rejim kesh muddati tugagunча kuchga kirmasdi).
        """
        await self._backend.set_setting(key, value)
        if key == "maintenance_mode":
            from app.services import maintenance

            maintenance.invalidate()

    # -- connections extended -----------------------------------------------

    async def all_connections(self) -> list[dict]:
        """Barcha ulanishlar (admin panel uchun)."""
        return await self._backend.all_connections()

    async def count_connections(self, enabled_only: bool = False) -> int:
        """Ulanishlar soni."""
        return await self._backend.count_connections(enabled_only)

    async def get_connection_stats(self, connection_id: str) -> dict:
        """Ulanish statistikasi (xabar soni, tahrir, o'chirish)."""
        return await self._backend.get_connection_stats(connection_id)

    async def all_connections_with_stats(self) -> list[dict]:
        """Barcha ulanishlar + har biri uchun hisoblar (BITTA so'rov).

        N+1 muammosini yo'q qiladi: ilgari har bir ulanish uchun alohida
        statistika so'rovi ketardi.
        """
        return await self._backend.all_connections_with_stats()

    async def delete_connection(self, connection_id: str) -> bool:
        """Ulanish yozuvini BUTUNLAY o'chiradi (owner-only, tasdiqdan keyin).

        Hodisalar (events) tarixi saqlanadi — faqat ulanish yozuvi o'chadi.
        """
        return await self._backend.delete_connection(connection_id)

    async def logs_page(
        self,
        *,
        limit: int = 10,
        offset: int = 0,
        min_severity: Optional[str] = None,
    ) -> list[dict]:
        """Activity log sahifasi (ixtiyoriy severity filtri bilan)."""
        return await self._backend.logs_page(limit, offset, min_severity)

    async def logs_page_count(self, *, min_severity: Optional[str] = None) -> int:
        return await self._backend.logs_page_count(min_severity=min_severity)

    # -- retention (tozalash) ----------------------------------------------

    async def prune_events_older_than(self, days: int) -> int:
        """``days`` kundan eski HODISALARNI o'chiradi (retention cleanup).

        Faqat events jadvali — faol RAM kesh va joriy ulanishlarga tegmaydi.
        """
        return await self._backend.prune_events_older_than(days)

    async def prune_activity_logs_older_than(self, days: int) -> int:
        """``days`` kundan eski activity log yozuvlarini o'chiradi."""
        return await self._backend.prune_activity_logs_older_than(days)

    async def prune_activity_logs(self, keep: int = 10_000) -> None:
        await self._backend.prune_activity_logs(keep)

    # -- broadcast progressi (reklama) --------------------------------------

    async def all_user_ids(
        self, limit: Optional[int] = None, offset: int = 0
    ) -> list[int]:
        """Faqat user IDlar (bo'laklab o'qish uchun — 30 000 ni RAMga
        yuklab olmaslik uchun batching)."""
        return await self._backend.all_user_ids(limit, offset)

    async def broadcast_seed(self, broadcast_id: str, user_ids: list[int]) -> int:
        """Broadcast oluvchilarni ``PENDING`` holatida yozadi (idempotent)."""
        return await self._backend.broadcast_seed(broadcast_id, user_ids)

    async def broadcast_mark(
        self, broadcast_id: str, user_id: int, status: str, error: str = ""
    ) -> None:
        """Bitta oluvchi holatini yangilaydi (PENDING / SENDING / SENT / FAILED / UNKNOWN)."""
        await self._backend.broadcast_mark(broadcast_id, user_id, status, error)

    async def broadcast_counts(self, broadcast_id: str) -> dict:
        """Broadcast recipient status counts, including UNKNOWN uncertain deliveries."""
        return await self._backend.broadcast_counts(broadcast_id)

    async def broadcast_ids(
        self,
        broadcast_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[int]:
        """Oluvchi IDlari (ixtiyoriy holat filtri bilan)."""
        return await self._backend.broadcast_ids(broadcast_id, status, limit, offset)

    async def broadcast_requeue_failed(self, broadcast_id: str) -> int:
        """``FAILED`` qatorlarni ``PENDING`` ga qaytaradi (Retry Failed)."""
        return await self._backend.broadcast_requeue_failed(broadcast_id)

    # -- analytics (haqiqiy baza ma'lumotlari) ------------------------------

    async def analytics_overview(self, since: str, until: str) -> dict:
        """Dashboard ko'rsatkichlari (foydalanuvchi/ulanish/xabar kesimi)."""
        return await self._backend.analytics_overview(since, until)

    async def analytics_series(
        self, since: str, until: str, bucket: str = "day"
    ) -> list[dict]:
        """Vaqt kesimi: ``hour`` (24 soat) yoki ``day`` (7/30 kun)."""
        return await self._backend.analytics_series(since, until, bucket)


async def _delete_local_fallback_user(user_id: int) -> None:
    """Prevent a deleted Postgres user from being resurrected by fallback sync."""
    import asyncio
    import sqlite3

    path = Path(settings.db_path)
    if not path.exists():
        return

    def _run() -> None:
        conn = sqlite3.connect(path, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN")
            conn.execute("DELETE FROM broadcast_recipients WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM events WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM connections WHERE user_id = ?", (user_id,))
            conn.execute("UPDATE activity_log SET user_id = NULL WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            row = conn.execute("SELECT value FROM bot_settings WHERE key = 'banned_users'").fetchone()
            if row and row[0]:
                try:
                    data = json.loads(row[0])
                    if isinstance(data, dict) and str(user_id) in data:
                        data.pop(str(user_id), None)
                        conn.execute(
                            "UPDATE bot_settings SET value = ?, updated_at = ? WHERE key = 'banned_users'",
                            (json.dumps(data), _now()),
                        )
                except (ValueError, TypeError):
                    pass
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    try:
        await asyncio.to_thread(_run)
    except Exception:
        logger.warning("Local SQLite fallback cleanup failed for deleted user=%s", user_id, exc_info=True)


class PostgresDatabase:
    """Supabase Postgres backend (asyncpg)."""

    def __init__(self) -> None:
        self._pool: Any = None  # asyncpg.Pool

    @property
    def pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("Database is not initialised - call init() first")
        return self._pool

    async def init(self) -> None:
        """Connect to Supabase, create tables, import old SQLite data once."""
        import asyncpg

        # ANIQ XATO usuli: foydalanuvchi ba'zan SUPABASE_DB_URL ga
        # LOYIHA SAYTINI (https://xxxx.supabase.co) yozib qo'yadi.
        # Bu http/https bo'lsa — asyncpg umuman DSN sifatida o'qiy olmaydi.
        # Bu yerda darhol tushunarli xato + tayyor TO'G'RI format beriladi.
        _scheme = settings.supabase_db_url.split(":", 1)[0].lower()
        if _scheme in ("http", "https"):
            _ref = ""
            try:
                from urllib.parse import urlparse

                host = urlparse(settings.supabase_db_url).hostname or ""
                _ref = host.split(".")[0]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(
                "SUPABASE_DB_URL noto'g'ri: bu LOYIHA SAYTI (https://...), "
                "baqa ULANISH MANZILI emas.\n\n"
                "Supabase Dashboard -> CONNECT (yashil tugma) -> "
                "'Connection pooling' -> URI ni nusxalang.  U shu ko'rinishda "
                "bo'ladi:\n\n"
                f"postgresql://postgres.{_ref}:[PAROLINGIZ]@aws-0-region.pooler.supabase.com:6543/postgres\n\n"
                "[PAROLINGIZ] joyiga Supabase bazasi PAROLINI yozing "
                "(unutasangiz: Settings -> Database -> Reset database password). "
                "env.txt dagi SUPABASE_KEY bot tomonidan ISHLATILMAYDI — "
                "uni o'chirishingiz mumkin."
            )

        try:
            self._pool = await asyncpg.create_pool(
                settings.supabase_db_url,
                # TEZLIK: Supabase uzoq regionda bo'lsa (masalan Sydney ~1.2 s)
                # har bir YANGI ulanish ham ~1.2 s oladi.  Shu sababli bir
                # nechta ulanish oldindan ochiladi — parallel so'rovlar
                # navbat kutmaydi (min_size=2 da 4 ta parallel so'rov 3.8 s
                # olardi, hozir ~1.2 s).
                min_size=5,
                max_size=10,
                timeout=30,
                # Supabase "Connection pooling" URI orqali ulanganda
                # PgBouncer (transaction mode) ishlatiladi — u prepared
                # statementlarni qo'llab-quvvatlamaydi.  Buni o'chirmasak
                # birinchi so'rovdayoq DuplicatePreparedStatementError
                # bilan yiqiladi.
                statement_cache_size=0,
            )
        except Exception as exc:  # noqa: BLE001 – show a human-friendly error
            raise RuntimeError(
                f"Could not connect to Supabase Postgres: {exc}\n"
                "Check SUPABASE_DB_URL in .env (use the 'Connection pooling' URI "
                "from the Supabase Dashboard -> Connect button)."
            ) from exc

        async with self.pool.acquire() as conn:
            # MUHIM MIGRATSIYA TARTIBI:
            # Eski Supabase bazasida ``events`` jadvali allaqachon mavjud
            # bo'lishi mumkin, lekin ``business_connection_id`` ustuni yo'q.
            # Agar butun SCHEMA avval ishga tushsa va undagi biror index/query
            # yangi ustunni ishlatsa, startup ``column ... does not exist`` bilan
            # yiqiladi.  Shuning uchun events jadvalini minimal to'liq ko'rinishda
            # avval kafolatlaymiz, keyin yangi ustunlarni ADD COLUMN qilamiz,
            # SHUNDAN KEYINGINA umumiy schema/indexlarni yaratamiz.
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id           BIGSERIAL PRIMARY KEY,
                    user_id      BIGINT NOT NULL,
                    sender_id    BIGINT,
                    business_connection_id TEXT,
                    chat_id      BIGINT,
                    chat_title   TEXT,
                    event_type   TEXT NOT NULL,
                    message_id   BIGINT,
                    details      TEXT NOT NULL DEFAULT '',
                    occurred_at  TEXT NOT NULL
                )
                """
            )
            await conn.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS sender_id BIGINT"
            )
            await conn.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS business_connection_id TEXT"
            )

            # Migratsiya haqiqatan qo'llanganini tekshiramiz.  Bu aniq xabar
            # beradi va noto'g'ri schema/search_path bo'lsa jimgina fallbackga
            # o'tib ketish o'rniga sababini ko'rsatadi.
            has_business_column = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'events'
                      AND column_name = 'business_connection_id'
                )
                """
            )
            if not has_business_column:
                raise RuntimeError(
                    "Supabase migration failed: events.business_connection_id "
                    "ustuni yaratilmadi. migrations/001_business_connection_id.sql "
                    "faylini Supabase SQL Editor'da bir marta RUN qiling."
                )

            await conn.execute(SCHEMA)
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_connection_message "
                "ON events (business_connection_id, chat_id, message_id)"
            )
            # Migratsiya: deploy (Railway) identifikatorlari — yangi deploy
            # eski deploy qulfini xavfsiz egallashi uchun (instance_lock.py).
            await conn.execute(
                "ALTER TABLE instance_lock ADD COLUMN IF NOT EXISTS service TEXT"
            )
            await conn.execute(
                "ALTER TABLE instance_lock ADD COLUMN IF NOT EXISTS deployment TEXT"
            )
            # Migration: add new tables if they don't exist
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_log (
                    id              BIGSERIAL PRIMARY KEY,
                    event_type      TEXT NOT NULL,
                    connection_id   TEXT,
                    user_id         BIGINT,
                    description     TEXT NOT NULL,
                    severity        TEXT NOT NULL DEFAULT 'INFO',
                    occurred_at     TEXT NOT NULL
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key         TEXT PRIMARY KEY,
                    value       TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
                """
            )
            # Add updated_at if the table was created before without it
            try:
                await conn.execute(
                    "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS updated_at TEXT NOT NULL DEFAULT ''"
                )
            except Exception:
                pass
            
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_log_time ON activity_log (occurred_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_log_type ON activity_log (event_type)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_connections_user ON connections (user_id)"
            )
            # Retention / analytics / qidiruv indekslari: event_type +
            # occurred_at bo'yicha VAQT ORALIG'I so'rovlari (tozalash ham
            # shu indeksdan foydalanadi).
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_type_time "
                "ON events (event_type, occurred_at)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_user_time "
                "ON events (user_id, occurred_at)"
            )
            # Activity log: ulanish bo'yicha filtr (admin panel).
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_log_conn "
                "ON activity_log (connection_id)"
            )
        await self._sync_sqlite_data()
        logger.info("Supabase Postgres ready (%s)", settings.supabase_host)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("Supabase connection closed")

    # -- SQLite fallback sinxronizatsiyasi (primary TIKLANGANDA) -----------
    async def _sync_sqlite_data(self) -> None:
        """SQLite'dagi ma'lumotlarni Supabasega ko'chiradi (idempotent).

        1. ``_import_sqlite_once`` — eski bir martalik import (orqaga
           moslik: mavjud bot.db bo'lsa uni BIR MARTA ko'chiradi).
        2. Kursorlar mavjud bo'lmasa — joriy yuqori nuqtaga o'rnatiladi
           (yuqoridagi import allaqachon ko'chirgan — dublikat bo'lmasin).
        3. ``sync_sqlite_into`` — SQLite fallback paytida yozilgan YANGI
           yozuvlarni ko'chiradi (kursorli, dublikatsiz).
        """
        from app.services import fallback_sync

        await self._import_sqlite_once()
        try:
            # Kursorlarni yuqori nuqtaga FAQAT bir martalik import HAQIQATAN
            # bajarilgan bo'lsa (marker bor) o'rnatamiz — aks holda mavjud
            # yozuvlar yutilib qolishi mumkin edi.  Marker yo'q bo'lsa
            # ``sync_sqlite_into`` noldan boshlab hammasini ko'chiradi.
            if not await self.sync_cursors() and await self._legacy_import_done():
                await fallback_sync.seed_cursors_at_high_water(self)
            stats = await fallback_sync.sync_sqlite_into(self)
        except Exception:  # noqa: BLE001 – sinxronizatsiya startupni to'xtatmasin
            logger.exception("SQLite fallback sinxronizatsiyasi xato berdi")
            return
        if any(stats.values()):
            logger.info("SQLite fallback sinxronizatsiyasi: %s", stats)

    async def _legacy_import_done(self) -> bool:
        """Eski bir martalik SQLite import bajarilganmi (marker bor)?"""
        try:
            return bool(
                await self._fetchval(
                    "SELECT COUNT(*) FROM supabase_migrations WHERE name = $1",
                    "sqlite_import_v1",
                )
            )
        except Exception:  # noqa: BLE001 – jadval yo'q bo'lsa import bo'lmagan
            return False

    async def sync_cursors(self) -> dict:
        """Saqlangan fallback sinxronizatsiya kursorlari."""
        from app.services import fallback_sync

        raw = await self.get_setting(fallback_sync.SYNC_CURSORS_KEY, "")
        return fallback_sync.cursors_from_json(raw)

    async def apply_sync_batch(self, cursors: dict, batch: dict) -> None:
        """Fallback batchini ATOMAR qo'llaydi + kursorlarni saqlaydi.

        Barcha yozuvlar + kursor BITTA tranzaksiyada commit qilinadi, shuning
        uchun xato/uzilish holatida qayta urinish dublikat yaratmaydi.

        MUHIM (eski ma'lumot qayta yozilmasligi uchun): users / connections /
        bot_settings faqat MANBA yozuvi PRIMARY'dagidan YANGIROQ bo'lsagina
        yangilanadi (``last_activity`` / ``connected_at``+``disconnected_at`` /
        ``updated_at`` bo'yicha).  Aks holda eski ``bot.db`` har startda
        bekor qilingan admin rollari, ulanish holati va sozlamalarni
        eskisiga qaytarib yuborardi.  Tenglikda PRIMARY yutadi.
        """
        from app.services import fallback_sync

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for u in batch.get("users") or ():
                    await conn.execute(
                        """
                        INSERT INTO users (user_id, username, first_name, last_name,
                                           connected_at, last_activity, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (user_id) DO UPDATE SET
                            username   = EXCLUDED.username,
                            first_name = EXCLUDED.first_name,
                            last_name  = EXCLUDED.last_name,
                            connected_at = COALESCE(EXCLUDED.connected_at, users.connected_at),
                            last_activity = COALESCE(EXCLUDED.last_activity, users.last_activity)
                        WHERE COALESCE(EXCLUDED.last_activity, '')
                              > COALESCE(users.last_activity, '')
                        """,
                        u.get("user_id"), u.get("username"), u.get("first_name"),
                        u.get("last_name"), u.get("connected_at"),
                        u.get("last_activity"), u.get("created_at") or _now(),
                    )
                for c in batch.get("connections") or ():
                    await conn.execute(
                        """
                        INSERT INTO connections (business_connection_id, user_id,
                            user_chat_id, is_enabled, connected_at, disconnected_at)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (business_connection_id) DO UPDATE SET
                            user_id         = EXCLUDED.user_id,
                            user_chat_id    = COALESCE(EXCLUDED.user_chat_id, connections.user_chat_id),
                            is_enabled      = EXCLUDED.is_enabled,
                            connected_at    = EXCLUDED.connected_at,
                            disconnected_at = EXCLUDED.disconnected_at
                        WHERE GREATEST(COALESCE(EXCLUDED.connected_at, ''),
                                       COALESCE(EXCLUDED.disconnected_at, ''))
                              > GREATEST(COALESCE(connections.connected_at, ''),
                                         COALESCE(connections.disconnected_at, ''))
                        """,
                        c.get("business_connection_id"), c.get("user_id"),
                        c.get("user_chat_id"), bool(c.get("is_enabled")),
                        c.get("connected_at"), c.get("disconnected_at"),
                    )
                for s in batch.get("settings") or ():
                    await conn.execute(
                        """
                        INSERT INTO bot_settings (key, value, updated_at)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (key) DO UPDATE SET
                            value = EXCLUDED.value,
                            updated_at = EXCLUDED.updated_at
                        WHERE EXCLUDED.updated_at > bot_settings.updated_at
                        """,
                        s.get("key"), s.get("value"), s.get("updated_at") or _now(),
                    )
                for e in batch.get("events") or ():
                    await conn.execute(
                        """
                        INSERT INTO events (user_id, sender_id, business_connection_id,
                                            chat_id, chat_title, event_type, message_id,
                                            details, occurred_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        e.get("user_id"), e.get("sender_id"),
                        e.get("business_connection_id"), e.get("chat_id"),
                        e.get("chat_title"), e.get("event_type"), e.get("message_id"),
                        e.get("details") or "", e.get("occurred_at") or _now(),
                    )
                for row in batch.get("activity_log") or ():
                    await conn.execute(
                        """
                        INSERT INTO activity_log (event_type, connection_id, user_id,
                                                  description, severity, occurred_at)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        """,
                        row.get("event_type"), row.get("connection_id"),
                        row.get("user_id"), row.get("description") or "",
                        row.get("severity") or "INFO",
                        row.get("occurred_at") or _now(),
                    )
                await conn.execute(
                    """
                    INSERT INTO bot_settings (key, value, updated_at)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (key) DO UPDATE SET
                        value = EXCLUDED.value,
                        updated_at = EXCLUDED.updated_at
                    """,
                    fallback_sync.SYNC_CURSORS_KEY, json.dumps(cursors), _now(),
                )

    # -- raw helpers ---------------------------------------------------------
    #
    # BARCHA so'rovlar shu 4 ta yordamchidan o'tadi — shuning uchun qayta
    # urinish (retry + backoff), ulanishni tiklash va health yozuvi shu
    # yerda, BITTA joyda amalga oshiriladi.

    async def _reconnect(self) -> None:
        """Uzilgan poolni qayta quradi (bir marta)."""
        try:
            if self._pool is not None:
                await self._pool.close()
        except Exception:  # noqa: BLE001 – eski pool allaqachon o'lgan
            pass
        self._pool = None
        db_health.health.record_reconnect()
        logger.warning("Supabase ulanishi tiklanmoqda (pool qayta qurilyapti)...")
        await self.init()

    async def _raw_fetch(self, op: str, kind: str, sql: str, params: tuple) -> Any:
        """Yagona ijro yo'li: retry -> pool tiklash -> yana retry."""
        import asyncpg

        async def run() -> Any:
            async with self.pool.acquire() as conn:
                if kind == "all":
                    rows = await conn.fetch(sql, *params)
                    return [dict(r) for r in rows]
                if kind == "one":
                    row = await conn.fetchrow(sql, *params)
                    return dict(row) if row else None
                if kind == "val":
                    return await conn.fetchval(sql, *params)
                await conn.execute(sql, *params)
                return 0

        try:
            return await _resilient(op, run)
        except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError) as exc:
            # Pool o'lgan bo'lishi mumkin: bir marta qayta ulanamiz va yana
            # urinamiz.  Bu yerda ham xato bo'lsa — yuqoriga ko'tariladi
            # (jim yutilmaydi).
            db_health.health.record_error(exc, op=f"{op}:reconnect")
            await self._reconnect()
            return await _resilient(op, run)

    async def _fetch_all(self, sql: str, *params: Any) -> list[dict]:
        return await self._raw_fetch("fetch_all", "all", sql, params)

    async def _fetch_one(self, sql: str, *params: Any) -> Optional[dict]:
        return await self._raw_fetch("fetch_one", "one", sql, params)

    async def _execute(self, sql: str, *params: Any) -> int:
        """Run a write query; returns 0 (auto ids are never needed here)."""
        return await self._raw_fetch("execute", "exec", sql, params)

    async def _fetchval(self, sql: str, *params: Any) -> Any:
        return await self._raw_fetch("fetchval", "val", sql, params)

    async def ping(self) -> bool:
        """Ulanish tirikmi? (health ekrani uchun yengil tekshiruv)"""
        value = await self._fetchval("SELECT 1")
        return value == 1

    # ======================================================================
    # USERS
    # ======================================================================

    async def upsert_user(
        self,
        user_id: int,
        username: Optional[str],
        first_name: Optional[str],
        last_name: Optional[str],
    ) -> None:
        """Insert the user if new, otherwise refresh profile fields."""
        now = _now()
        await self._execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name,
                               connected_at, last_activity, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (user_id) DO UPDATE SET
                username   = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name  = EXCLUDED.last_name,
                last_activity = EXCLUDED.last_activity
            """,
            user_id,
            username,
            first_name,
            last_name,
            now,
            now,
            now,
        )

    async def touch_user(self, user_id: int) -> None:
        await self._execute(
            "UPDATE users SET last_activity = $1 WHERE user_id = $2",
            _now(),
            user_id,
        )

    async def get_user(self, user_id: int) -> Optional[dict]:
        return await self._fetch_one("SELECT * FROM users WHERE user_id = $1", user_id)

    async def all_users(self) -> list[dict]:
        """All users, newest first."""
        return await self._fetch_all("SELECT * FROM users ORDER BY created_at DESC")

    async def users_page(self, search: str = "", *, limit: int = 50, offset: int = 0) -> list[dict]:
        search = str(search or "").strip()
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        where = ""
        params: list[Any] = []
        if search:
            params = [f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%"]
            where = (
                "WHERE CAST(u.user_id AS TEXT) ILIKE $1 "
                "OR COALESCE(u.username,'') ILIKE $2 "
                "OR COALESCE(u.first_name,'') ILIKE $3 "
                "OR COALESCE(u.last_name,'') ILIKE $4"
            )
            limit_p, offset_p = 5, 6
        else:
            limit_p, offset_p = 1, 2
            params = []
        params.extend([limit, offset])
        return await self._fetch_all(
            f"""
            SELECT u.*,
                   COUNT(c.business_connection_id) AS connections_count,
                   COUNT(c.business_connection_id) FILTER (WHERE c.is_enabled = TRUE) AS active_connections
            FROM users u
            LEFT JOIN connections c ON c.user_id = u.user_id
            {where}
            GROUP BY u.user_id
            ORDER BY u.last_activity DESC NULLS LAST, u.created_at DESC
            LIMIT ${limit_p} OFFSET ${offset_p}
            """,
            *params,
        )

    async def count_users_filtered(self, search: str = "") -> int:
        search = str(search or "").strip()
        if not search:
            return int(await self._fetchval("SELECT COUNT(*) FROM users") or 0)
        pattern = f"%{search}%"
        return int(await self._fetchval(
            """SELECT COUNT(*) FROM users u
               WHERE CAST(u.user_id AS TEXT) ILIKE $1
                  OR COALESCE(u.username,'') ILIKE $1
                  OR COALESCE(u.first_name,'') ILIKE $1
                  OR COALESCE(u.last_name,'') ILIKE $1""", pattern
        ) or 0)

    async def delete_user(self, user_id: int) -> bool:
        """Operational user data-ni atomar o'chiradi, audit tarixini saqlab qoladi."""
        user_id = int(user_id)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                exists = await conn.fetchval("SELECT 1 FROM users WHERE user_id = $1", user_id)
                if not exists:
                    return False
                await conn.execute("DELETE FROM broadcast_recipients WHERE user_id = $1", user_id)
                await conn.execute("DELETE FROM events WHERE user_id = $1", user_id)
                await conn.execute("DELETE FROM connections WHERE user_id = $1", user_id)
                await conn.execute("UPDATE activity_log SET user_id = NULL WHERE user_id = $1", user_id)
                await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
        await _delete_local_fallback_user(user_id)
        return True

    async def online_users(self, window_seconds: int = 120) -> list[dict]:
        """Users whose last_activity is within the given window.

        last_activity is ISO TEXT -> compare with generated ISO threshold.
        """
        threshold = (local_now() - timedelta(seconds=window_seconds)).isoformat(
            timespec="seconds"
        )
        return await self._fetch_all(
            """
            SELECT * FROM users
            WHERE last_activity >= $1
            ORDER BY last_activity DESC
            """,
            threshold,
        )

    async def count_users(self) -> int:
        return int(await self._fetchval("SELECT COUNT(*) FROM users"))

    async def count_online(self, window_seconds: int = 120) -> int:
        threshold = (local_now() - timedelta(seconds=window_seconds)).isoformat(
            timespec="seconds"
        )
        return int(
            await self._fetchval(
                "SELECT COUNT(*) FROM users WHERE last_activity >= $1",
                threshold,
            )
        )

    async def user_stats(self, user_id: int) -> dict:
        """Statistika ekranining barcha raqamlari — BITTA so'rov.

        TEZLIK: Supabase uzoqda bo'lsa har bir so'rov ~1.2 s.  Ekran 7 ta
        alohida so'rovdan (4 s) bitta so'rovga o'tkazildi (~1.2 s).
        """
        row = await self._fetch_one(
            """
            SELECT
              (SELECT COUNT(*) FROM users)                        AS users_total,
              (SELECT COUNT(*) FROM events WHERE user_id = $1)     AS events_total,
              (SELECT COUNT(*) FROM events
                 WHERE user_id = $1 AND event_type = 'edit')       AS edits,
              (SELECT COUNT(*) FROM events
                 WHERE user_id = $1 AND event_type = 'delete')     AS deletes,
              (SELECT COUNT(*) FROM events
                 WHERE user_id = $1 AND event_type = 'delete_media') AS deletes_media,
              (SELECT COUNT(*) FROM connections
                 WHERE user_id = $1 AND is_enabled = TRUE)         AS active_connections
            """,
            user_id,
        )
        return {
            "users_total": int((row or {}).get("users_total") or 0),
            "events_total": int((row or {}).get("events_total") or 0),
            "edits": int((row or {}).get("edits") or 0),
            "deletes": int((row or {}).get("deletes") or 0),
            "deletes_media": int((row or {}).get("deletes_media") or 0),
            "active_connections": int((row or {}).get("active_connections") or 0),
        }

    async def has_active_connection(self, user_id: int) -> bool:
        """/start uchun: faol biznes-ulanish bormi (1 so'rov)."""
        value = await self._fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM connections WHERE user_id = $1 AND is_enabled = TRUE
            )
            """,
            user_id,
        )
        return _b(value)

    # ======================================================================
    # INSTANCE LOCK
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
        """Qulfni ATOMIK olish (yoki mavjud egasini qaytarish).

        Bir SQL bayonotda: yozuv yo'q bo'lsa yaratamiz, mavjud bo'lsa
        FAQAT quyidagi hollarda egallaymiz:

        * egasi o'zimiz bo'lsak;
        * heartbeat ``stale_seconds`` dan eski bo'lsa (nusxa o'lgan);
        * joriy nusxa BOSHQARILADIGAN deploy (``service`` berilgan) bo'lsa
          va qulfdagi yozuv ham shu servis (yoki servissiz eski yozuv)
          bo'lsa — platforma eski nusxani almashtiradi, shuning uchun
          uning hali "yangi" heartbeat'i startni to'xtatmasligi kerak.
          Bu holatda eski nusxa o'z heartbeat'ida qulfni yo'qotib,
          pollingni to'xtatadi — ikki nusxa birga qolmaydi.
          Boshqa servis (``service`` boshqacha) qulfini tortib OLMAYMIZ.
        """
        now = _now()
        stale_before = (
            local_now() - timedelta(seconds=stale_seconds)
        ).isoformat(timespec="seconds")
        owned = await self._fetchval(
            """
            INSERT INTO instance_lock
                (id, instance, host, pid, service, deployment,
                 started_at, heartbeat_at)
            VALUES ('bot', $1, $2, $3, $4, $5, $6, $6)
            ON CONFLICT (id) DO UPDATE SET
                instance     = EXCLUDED.instance,
                host         = EXCLUDED.host,
                pid          = EXCLUDED.pid,
                service      = EXCLUDED.service,
                deployment   = EXCLUDED.deployment,
                started_at   = EXCLUDED.started_at,
                heartbeat_at = EXCLUDED.heartbeat_at
            WHERE instance_lock.instance = EXCLUDED.instance
               OR instance_lock.heartbeat_at <= $7
               OR (
                    EXCLUDED.service IS NOT NULL
                    AND (instance_lock.service IS NULL
                         OR instance_lock.service = EXCLUDED.service)
                  )
            RETURNING instance
            """,
            instance,
            host,
            pid,
            service,
            deployment,
            now,
            stale_before,
        )
        if owned:
            return None
        return await self._fetch_one(
            "SELECT * FROM instance_lock WHERE id = 'bot'"
        )

    async def heartbeat_instance_lock(self, instance: str) -> bool:
        """Heartbeat yangilash; ``False`` — qulfni boshqa nusxa olib qo'ydi."""
        value = await self._fetchval(
            """
            UPDATE instance_lock SET heartbeat_at = $1
            WHERE id = 'bot' AND instance = $2
            RETURNING 1
            """,
            _now(),
            instance,
        )
        return bool(value)

    async def release_instance_lock(self, instance: str) -> None:
        await self._execute(
            "DELETE FROM instance_lock WHERE id = 'bot' AND instance = $1",
            instance,
        )

    # ======================================================================
    # EVENTS (captured activity)
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
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            user_id,
            chat_id,
            chat_title,
            event_type,
            message_id,
            details,
            sender_id,
            business_connection_id,
            _now(),
        )

    async def recent_events(self, limit: int = 20, offset: int = 0) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM events ORDER BY id DESC LIMIT $1 OFFSET $2",
            max(1, int(limit)), max(0, int(offset)),
        )

    async def events_page(
        self,
        event_types: Optional[list[str]] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[dict]:
        """Turi bo'yicha filtrlab sahifalab o'qish (idx_events_type_time)."""
        params: list[Any] = []
        sql = "SELECT * FROM events"
        if event_types:
            params.append(list(event_types))
            sql += " WHERE event_type = ANY($1)"
        params.extend([max(1, int(limit)), max(0, int(offset))])
        sql += f" ORDER BY id DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}"
        return await self._fetch_all(sql, *params)

    async def count_events_multi(self, event_types: Optional[list[str]] = None) -> int:
        if event_types:
            return int(await self._fetchval(
                "SELECT COUNT(*) FROM events WHERE event_type = ANY($1)",
                list(event_types),
            ))
        return int(await self._fetchval("SELECT COUNT(*) FROM events"))

    async def count_events(
        self, event_type: Optional[str] = None, since: Optional[datetime] = None
    ) -> int:
        sql = "SELECT COUNT(*) FROM events WHERE TRUE"
        params: list[Any] = []
        if event_type:
            params.append(event_type)
            sql += f" AND event_type = ${len(params)}"
        if since:
            params.append(since.isoformat(timespec="seconds"))
            sql += f" AND occurred_at >= ${len(params)}"
        return int(await self._fetchval(sql, *params))

    async def count_user_events(self, user_id: int, event_type: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) FROM events WHERE user_id = $1"
        params: list[Any] = [user_id]
        if event_type:
            params.append(event_type)
            sql += f" AND event_type = ${len(params)}"
        return int(await self._fetchval(sql, *params))

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
        sql += f" ORDER BY id DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}"
        return await self._fetch_all(sql, *params)

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
        return int(await self._fetchval(sql, *params))

    async def event_stats_since(self, since: Optional[datetime] = None) -> dict:
        """received / edited / deleted — BITTA so'rovda (analytics)."""
        if since is None:
            row = await self._fetch_one(
                """
                SELECT
                  COUNT(*) FILTER (WHERE event_type NOT IN
                      ('edit','delete','delete_media','connection')) AS received,
                  COUNT(*) FILTER (WHERE event_type = 'edit')          AS edited,
                  COUNT(*) FILTER (WHERE event_type IN
                      ('delete','delete_media'))                        AS deleted
                FROM events
                """
            )
        else:
            stamp = since.isoformat(timespec="seconds")
            row = await self._fetch_one(
                """
                SELECT
                  COUNT(*) FILTER (WHERE event_type NOT IN
                      ('edit','delete','delete_media','connection')) AS received,
                  COUNT(*) FILTER (WHERE event_type = 'edit')          AS edited,
                  COUNT(*) FILTER (WHERE event_type IN
                      ('delete','delete_media'))                        AS deleted
                FROM events WHERE occurred_at >= $1
                """,
                stamp,
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
                WHERE chat_id = $2 AND message_id = $3
                  AND (
                    business_connection_id = $1
                    OR (
                      business_connection_id IS NULL
                      AND user_id IN (
                        SELECT user_id FROM connections
                        WHERE business_connection_id = $1
                      )
                    )
                  )
                ORDER BY CASE WHEN business_connection_id = $1 THEN 1 ELSE 0 END DESC, id DESC
                LIMIT 1
                """,
                business_connection_id, chat_id, message_id,
            )
        return await self._fetch_one(
            """
            SELECT * FROM events
            WHERE chat_id = $1 AND message_id = $2
            ORDER BY id DESC LIMIT 1
            """,
            chat_id, message_id,
        )

    async def update_event_details(
        self, chat_id: int, message_id: int, details: str,
        business_connection_id: Optional[str] = None,
    ) -> bool:
        """Faqat ENG OXIRGI mos yozuvni yangilaydi."""
        if business_connection_id:
            n = await self._fetchval(
                """
                UPDATE events SET details = $4
                WHERE id = (
                    SELECT id FROM events
                    WHERE chat_id = $2 AND message_id = $3
                      AND (
                        business_connection_id = $1
                        OR (
                          business_connection_id IS NULL
                          AND user_id IN (
                            SELECT user_id FROM connections
                            WHERE business_connection_id = $1
                          )
                        )
                      )
                    ORDER BY CASE WHEN business_connection_id = $1 THEN 1 ELSE 0 END DESC, id DESC
                    LIMIT 1
                )
                RETURNING 1
                """,
                business_connection_id, chat_id, message_id, details,
            )
        else:
            n = await self._fetchval(
                """
                UPDATE events SET details = $3
                WHERE id = (
                    SELECT id FROM events
                    WHERE chat_id = $1 AND message_id = $2
                    ORDER BY id DESC LIMIT 1
                )
                RETURNING 1
                """,
                chat_id, message_id, details,
            )
        return bool(n)

    async def prune_events(self, keep: int = 20_000) -> None:
        """Housekeeping: keep only the newest `keep` rows."""
        await self._execute(
            """
            DELETE FROM events
            WHERE id NOT IN (
                SELECT id FROM events ORDER BY id DESC LIMIT $1
            )
            """,
            keep,
        )

    async def prune_events_older_than(self, days: int) -> int:
        """Retention: ``days`` kundan eski hodisalarni o'chiradi (index scan)."""
        days = max(1, int(days))
        cutoff = (local_now() - timedelta(days=days)).isoformat(timespec="seconds")
        return await self._delete_rowcount(
            "DELETE FROM events WHERE occurred_at < $1", cutoff
        )

    async def prune_activity_logs_older_than(self, days: int) -> int:
        """Retention: eski activity log yozuvlarini o'chiradi."""
        days = max(1, int(days))
        cutoff = (local_now() - timedelta(days=days)).isoformat(timespec="seconds")
        return await self._delete_rowcount(
            "DELETE FROM activity_log WHERE occurred_at < $1", cutoff
        )

    async def _delete_rowcount(self, sql: str, *params: Any) -> int:
        """DELETE natijasidagi qatorlar soni (asyncpg status satridan).

        asyncpg ``execute`` DELETE/UPDATE uchun "DELETE 42" ko'rinishidagi
        status qaytaradi — shundan o'chirilgan qatorlar soni olinadi.
        """
        async def run() -> int:
            async with self.pool.acquire() as conn:
                status = await conn.execute(sql, *params)
            try:
                return int(str(status).rsplit(" ", 1)[-1])
            except (ValueError, AttributeError):
                return 0

        return int(await _resilient("delete_rowcount", run) or 0)

    # ======================================================================
    # BUSINESS CONNECTIONS
    # ======================================================================

    async def upsert_connection(
        self,
        business_connection_id: str,
        user_id: int,
        is_enabled: bool,
        user_chat_id: Optional[int] = None,
    ) -> None:
        now = _now()
        existing = await self._fetch_one(
            "SELECT * FROM connections WHERE business_connection_id = $1",
            business_connection_id,
        )
        if existing is None:
            await self._execute(
                """
                INSERT INTO connections
                    (business_connection_id, user_id, user_chat_id, is_enabled, connected_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                business_connection_id,
                user_id,
                user_chat_id,
                is_enabled,
                now,
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
                SET is_enabled = $1, connected_at = $2, disconnected_at = $3,
                    user_chat_id = COALESCE($4, user_chat_id)
                WHERE business_connection_id = $5
                """,
                is_enabled,
                connected_at,
                disconnected_at,
                user_chat_id,
                business_connection_id,
            )

    async def get_connection(self, business_connection_id: str) -> Optional[dict]:
        return await self._fetch_one(
            "SELECT * FROM connections WHERE business_connection_id = $1",
            business_connection_id,
        )

    async def connected_user_ids(self) -> list[int]:
        rows = await self._fetch_all(
            "SELECT DISTINCT user_id FROM connections WHERE is_enabled = TRUE"
        )
        return [row["user_id"] for row in rows]

    async def connections_for_user(self, user_id: int) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM connections WHERE user_id = $1 ORDER BY connected_at DESC",
            user_id,
        )

    # ======================================================================
    # ONE-TIME SQLITE -> SUPABASE IMPORT
    # ======================================================================

    # ======================================================================
    # ACTIVITY LOG
    # ======================================================================

    async def add_activity_log(
        self,
        event_type: str,
        description: str,
        *,
        connection_id: Optional[str] = None,
        user_id: Optional[int] = None,
        severity: str = "INFO",
    ) -> None:
        await self._execute(
            """
            INSERT INTO activity_log
                (event_type, connection_id, user_id, description, severity, occurred_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            event_type, connection_id, user_id, description, severity, _now(),
        )

    async def recent_activity_logs(self, limit: int = 50) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM activity_log ORDER BY occurred_at DESC LIMIT $1", limit
        )

    async def count_activity_logs(self) -> int:
        return int(await self._fetchval("SELECT COUNT(*) FROM activity_log"))

    async def logs_page(
        self,
        limit: int = 10,
        offset: int = 0,
        min_severity: Optional[str] = None,
    ) -> list[dict]:
        """Activity log sahifasi (ixtiyoriy: faqat WARNING+)."""
        if min_severity:
            return await self._fetch_all(
                """
                SELECT * FROM activity_log
                WHERE severity = ANY($1)
                ORDER BY id DESC LIMIT $2 OFFSET $3
                """,
                _severity_levels(min_severity),
                max(1, int(limit)),
                max(0, int(offset)),
            )
        return await self._fetch_all(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT $1 OFFSET $2",
            max(1, int(limit)), max(0, int(offset)),
        )

    async def logs_page_count(self, *, min_severity: Optional[str] = None) -> int:
        """Filtrga MOS total (logs_page() bilan AYNAN bir xil WHERE).

        Bu metod bo'lmasa WARNING filtri Supabase'da "AttributeError"
        berardi (SQLite'da bor edi, Postgres'da yo'q edi).
        """
        if min_severity:
            return int(await self._fetchval(
                "SELECT COUNT(*) FROM activity_log WHERE severity = ANY($1)",
                _severity_levels(min_severity),
            ))
        return int(await self._fetchval("SELECT COUNT(*) FROM activity_log"))

    async def prune_activity_logs(self, keep: int = 10_000) -> None:
        await self._execute(
            """
            DELETE FROM activity_log
            WHERE id NOT IN (
                SELECT id FROM activity_log ORDER BY id DESC LIMIT $1
            )
            """,
            keep,
        )

    # ======================================================================
    # BROADCAST PROGRESSI (reklama) + ANALYTICS
    # ======================================================================

    async def all_user_ids(
        self, limit: Optional[int] = None, offset: int = 0
    ) -> list[int]:
        """Faqat IDlar (batching) — 30 000 foydalanuvchini RAMga yuklamaslik."""
        if limit is None:
            rows = await self._fetch_all(
                "SELECT user_id FROM users ORDER BY user_id ASC"
            )
        else:
            rows = await self._fetch_all(
                "SELECT user_id FROM users ORDER BY user_id ASC LIMIT $1 OFFSET $2",
                max(1, int(limit)), max(0, int(offset)),
            )
        return [int(row["user_id"]) for row in rows]

    async def broadcast_seed(self, broadcast_id: str, user_ids: list[int]) -> int:
        """Oluvchilarni PENDING holatida yozadi (takroriy yozuv qo'shilmaydi)."""
        if not user_ids:
            return 0
        stamp = _now()
        async def run() -> int:
            async with self.pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO broadcast_recipients
                        (broadcast_id, user_id, status, error, updated_at)
                    VALUES ($1, $2, 'PENDING', '', $3)
                    ON CONFLICT (broadcast_id, user_id) DO NOTHING
                    """,
                    [(broadcast_id, int(uid), stamp) for uid in user_ids],
                )
            return len(user_ids)

        return int(await _resilient("broadcast_seed", run) or 0)

    async def broadcast_mark(
        self, broadcast_id: str, user_id: int, status: str, error: str = ""
    ) -> None:
        await self._execute(
            """
            INSERT INTO broadcast_recipients
                (broadcast_id, user_id, status, error, updated_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (broadcast_id, user_id) DO UPDATE SET
                status = EXCLUDED.status,
                error = EXCLUDED.error,
                updated_at = EXCLUDED.updated_at
            """,
            broadcast_id, int(user_id), str(status), str(error)[:200], _now(),
        )

    async def broadcast_counts(self, broadcast_id: str) -> dict:
        rows = await self._fetch_all(
            """
            SELECT status, COUNT(*) AS n FROM broadcast_recipients
            WHERE broadcast_id = $1 GROUP BY status
            """,
            broadcast_id,
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
                WHERE broadcast_id = $1 AND status = $2
                ORDER BY user_id ASC LIMIT $3 OFFSET $4
                """,
                broadcast_id, status, max(1, int(limit)), max(0, int(offset)),
            )
        else:
            rows = await self._fetch_all(
                """
                SELECT user_id FROM broadcast_recipients
                WHERE broadcast_id = $1
                ORDER BY user_id ASC LIMIT $2 OFFSET $3
                """,
                broadcast_id, max(1, int(limit)), max(0, int(offset)),
            )
        return [int(row["user_id"]) for row in rows]

    async def broadcast_requeue_failed(self, broadcast_id: str) -> int:
        return await self._delete_rowcount(
            """
            UPDATE broadcast_recipients SET status = 'PENDING', error = ''
            WHERE broadcast_id = $1 AND status = 'FAILED'
            """,
            broadcast_id,
        )

    async def analytics_overview(self, since: str, until: str) -> dict:
        sql, params = analytics_overview_sql(since, until)
        return analytics_row(await self._fetch_one(sql, *params))

    async def analytics_series(
        self, since: str, until: str, bucket: str = "day"
    ) -> list[dict]:
        sql, params = analytics_series_sql(since, until, bucket)
        rows = await self._fetch_all(sql, *params)
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

    # ======================================================================
    # BOT SETTINGS
    # ======================================================================

    async def get_setting(self, key: str, default: str = "") -> str:
        row = await self._fetch_one(
            "SELECT value FROM bot_settings WHERE key = $1", key
        )
        return str(row["value"]) if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self._execute(
            """
            INSERT INTO bot_settings (key, value, updated_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = EXCLUDED.updated_at
            """,
            key, value, _now(),
        )

    # ======================================================================
    # CONNECTIONS EXTENDED
    # ======================================================================

    async def all_connections(self) -> list[dict]:
        """Admin panel: barcha ulanishlar (egasi nomi bilan)."""
        return await self._fetch_all(
            """
            SELECT c.*,
                   u.username   AS owner_username,
                   u.first_name AS owner_first_name
            FROM connections c
            LEFT JOIN users u ON u.user_id = c.user_id
            ORDER BY c.connected_at DESC NULLS LAST
            """
        )

    async def all_connections_with_stats(self) -> list[dict]:
        """Barcha ulanishlar + har biri uchun hisoblar — BITTA so'rov.

        N+1 ni yo'q qiladi: received / edited / deleted va oxirgi xabar
        vaqti har bir ulanish uchun bir yo'la hisoblanadi (events.user_id
        = ulanish egasi bo'yicha agregatsiya).
        """
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
            ORDER BY c.connected_at DESC NULLS LAST
            """
        )

    async def delete_connection(self, connection_id: str) -> bool:
        """Ulanish yozuvini o'chiradi (owner-only amal, tasdiqdan keyin)."""
        return bool(await self._delete_rowcount(
            "DELETE FROM connections WHERE business_connection_id = $1",
            connection_id,
        ))

    async def count_connections(self, enabled_only: bool = False) -> int:
        if enabled_only:
            return int(await self._fetchval(
                "SELECT COUNT(*) FROM connections WHERE is_enabled = TRUE"
            ))
        return int(await self._fetchval("SELECT COUNT(*) FROM connections"))

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
            WHERE c.business_connection_id = $1
            """,
            connection_id,
        )
        return {
            "received": int((row or {}).get("received") or 0),
            "edited":   int((row or {}).get("edited") or 0),
            "deleted":  int((row or {}).get("deleted") or 0),
        }

    async def _import_sqlite_once(self) -> None:
        """If ./bot.db exists and we haven't imported before, copy data over.

        Kept tolerant: a missing/corrupt SQLite file is skipped with a log
        line, never blocks startup.
        """
        sqlite_path = Path(settings.db_path)
        if not sqlite_path.exists():
            return

        already = await self._fetchval(
            "SELECT COUNT(*) FROM supabase_migrations WHERE name = $1",
            "sqlite_import_v1",
        )
        if already:
            return

        logger.info("Found old bot.db - importing data into Supabase once...")
        try:
            import aiosqlite

            conn = await aiosqlite.connect(settings.db_path)
            conn.row_factory = aiosqlite.Row

            async def _rows(table: str) -> list[dict]:
                """Read a table if it exists (old DBs may lack some tables)."""
                try:
                    cursor = await conn.execute(f"SELECT * FROM {table}")
                except Exception:  # noqa: BLE001 – table missing
                    return []
                rows = [dict(r) for r in await cursor.fetchall()]
                await cursor.close()
                return rows

            try:
                users = await _rows("users")
                events = await _rows("events")
                connections = await _rows("connections")
            finally:
                await conn.close()

            async with self.pool.acquire() as pg:
                for u in users:
                    await pg.execute(
                        """
                        INSERT INTO users (user_id, username, first_name, last_name,
                            connected_at, last_activity, created_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7)
                        ON CONFLICT (user_id) DO NOTHING
                        """,
                        u["user_id"], u.get("username"), u.get("first_name"),
                        u.get("last_name"), u.get("connected_at"),
                        u.get("last_activity"), u["created_at"],
                    )
                for e in events:
                    await pg.execute(
                        """
                        INSERT INTO events (user_id, chat_id, chat_title, event_type,
                                            message_id, details, sender_id,
                                            business_connection_id, occurred_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                        """,
                        e["user_id"], e.get("chat_id"), e.get("chat_title"),
                        e["event_type"], e.get("message_id"), e.get("details") or "",
                        e.get("sender_id"), e.get("business_connection_id"),
                        e["occurred_at"],
                    )
                for c in connections:
                    await pg.execute(
                        """
                        INSERT INTO connections (business_connection_id, user_id,
                            user_chat_id, is_enabled, connected_at, disconnected_at)
                        VALUES ($1,$2,$3,$4,$5,$6)
                        ON CONFLICT (business_connection_id) DO NOTHING
                        """,
                        c["business_connection_id"], c["user_id"], c.get("user_chat_id"),
                        bool(c.get("is_enabled")), c.get("connected_at"),
                        c.get("disconnected_at"),
                    )
                await pg.execute(
                    "INSERT INTO supabase_migrations (name, applied_at) "
                    "VALUES ('sqlite_import_v1', $1) ON CONFLICT DO NOTHING",
                    _now(),
                )

            logger.info(
                "Import finished: %s users, %s events, %s connections",
                len(users), len(events), len(connections),
            )
        except Exception:  # noqa: BLE001 – never block startup on import
            logger.exception("SQLite import failed (bot continues with Supabase data)")


# Single shared instance – import `db` everywhere.
db = Database()
