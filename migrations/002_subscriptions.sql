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
