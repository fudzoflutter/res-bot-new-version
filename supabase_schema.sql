-- ============================================================================
-- Telegram Activity Monitor Bot — Supabase schema
--
-- QANDAY ISHLATILADI (2 usul):
--
-- 1) AVTOMATIK (tavsiya etiladi): shunchaki botni ishga tushiring —
--    app/database.py dagi PostgresDatabase.init() aynan shu DDL ni
--    bajarmoqda (CREATE TABLE IF NOT EXISTS — qayta ishga xavfsiz).
--
-- 2) QO'LDA: Supabase Dashboard → SQL Editor → New query → shu fayl
--    tarkibini to'liq joylashtiring → RUN.
--
-- Fayl idempotent: bir necha marta ishga tushirilsa, hech narsani
-- buzmaydi (mavjud jadvallar/indexlar qayta yaratilmaydi).
--
-- DIQQAT: bot quyidagi jadvallar bilan ishlaydi — users, events,
-- connections, activity_log (admin Activity Log), bot_settings (retention
-- va maintenance), instance_lock (qaysi nusxa polling qilmoqda).
-- Foydalanuvchi kirish tizimi (access: ruxsat / rad / ban) OLIB
-- TASHLANGAN — admin panel server-side owner/admin/moderator/viewer rollari
-- va permission matritsasi bilan ishlaydi. Bu DDL mavjud jadvallarni
-- O'CHIRMAYDI; eski access /
-- plans / payments jadvallari qolaversa hech narsaga zarari yo'q.
-- ============================================================================

CREATE TABLE IF NOT EXISTS activity_log (
    id              BIGSERIAL PRIMARY KEY,
    event_type      TEXT NOT NULL,
    connection_id   TEXT,
    user_id         BIGINT,
    description     TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'INFO',
    occurred_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

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
    details      TEXT NOT NULL DEFAULT '',
    occurred_at  TEXT NOT NULL
);


-- Eski bazalarda ustun bo'lmasa qo'shiladi.
ALTER TABLE events ADD COLUMN IF NOT EXISTS sender_id BIGINT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS business_connection_id TEXT;

CREATE TABLE IF NOT EXISTS connections (
    business_connection_id TEXT PRIMARY KEY,
    user_id                BIGINT NOT NULL,
    user_chat_id           BIGINT,
    is_enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    connected_at           TEXT,
    disconnected_at        TEXT
);

-- Bir vaqtda faqat BITTA nusxa polling qilishi uchun qulf (heartbeat).
-- service/deployment: Railway (RAILWAY_SERVICE_ID / RAILWAY_DEPLOYMENT_ID)
-- qiymatlari — SHU SERVISning yangi deployi eski deploy qulfini xavfsiz
-- egallashi uchun (app/services/instance_lock.py).
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

CREATE INDEX IF NOT EXISTS idx_events_time         ON events (occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_type         ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_user         ON events (user_id);
CREATE INDEX IF NOT EXISTS idx_events_chat_message ON events (chat_id, message_id);
CREATE INDEX IF NOT EXISTS idx_events_connection_message
    ON events (business_connection_id, chat_id, message_id);
-- Retention / analytics / search: tur bo'yicha vaqt oralig'i (occurred_at
-- bo'yicha tozalash ham shu indekslardan foydalanadi).
CREATE INDEX IF NOT EXISTS idx_events_type_time    ON events (event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_user_time    ON events (user_id, occurred_at);

-- Aktivlik jurnali: connection bo'yicha filtr (admin panel) + tozalash.
CREATE INDEX IF NOT EXISTS idx_activity_log_conn   ON activity_log (connection_id);
CREATE INDEX IF NOT EXISTS idx_activity_log_time   ON activity_log (occurred_at);
CREATE INDEX IF NOT EXISTS idx_activity_log_type   ON activity_log (event_type);


-- Broadcast progress: har bir qabul qiluvchi uchun holat saqlanadi.
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

-- Bir martalik SQLite import markeri (bot o'zi boshqaradi).
CREATE TABLE IF NOT EXISTS supabase_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Premium subscriptions + manual card payments.
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id       BIGINT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'ACTIVE',
    starts_at     TEXT,
    expires_at    TEXT,
    is_lifetime   BOOLEAN NOT NULL DEFAULT FALSE,
    granted_by    BIGINT,
    note          TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_requests (
    id              TEXT PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    plan_days       INTEGER NOT NULL,
    amount          BIGINT NOT NULL,
    receipt_file_id TEXT NOT NULL,
    receipt_kind    TEXT NOT NULL DEFAULT 'photo',
    status          TEXT NOT NULL DEFAULT 'PENDING',
    created_at      TEXT NOT NULL,
    reviewed_at     TEXT,
    reviewed_by     BIGINT,
    reject_reason   TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_expiry
    ON subscriptions (status, expires_at);
CREATE INDEX IF NOT EXISTS idx_payment_requests_status
    ON payment_requests (status, created_at);
CREATE INDEX IF NOT EXISTS idx_payment_requests_user
    ON payment_requests (user_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_requests_one_pending
    ON payment_requests (user_id) WHERE status = 'PENDING';
