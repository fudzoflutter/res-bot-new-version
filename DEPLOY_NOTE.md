# Deploy note — Supabase / Railway

## Reported `business_connection_id` error

This build fixes the startup order that caused old Supabase databases to fail with:

```text
column "business_connection_id" does not exist
```

On startup the bot now creates/migrates `events.business_connection_id` **before**
the general schema and `idx_events_connection_message` index are applied.
The migration is idempotent, so an existing database is safe.

Normally you only need to deploy this build and restart the Railway service.

If the Supabase database role used by `SUPABASE_DB_URL` does not have permission
to `ALTER TABLE`, run `migrations/001_business_connection_id.sql` once from the
Supabase SQL Editor, then restart Railway.

## Before production

1. Keep real secrets only in Railway Variables / your secret store; do not add a real `.env` to Git.
2. Deploy this project as one polling instance.
3. After startup, confirm the admin Database/Bot Health screens show Supabase as healthy.
4. Test Admin add/change/remove, Ban/Unban, Hold ON/OFF, Broadcast Preview/Test/Send/Retry, Users pagination/search, Analytics, Verify Connections, and Retention once with a test account.
5. In a normal development environment run:

```bash
python -m pip install -r requirements.txt
python -m pytest -q
```

The analysis container used for this patch did not include `aiogram`, `aiosqlite`,
or `asyncpg`, so the complete runtime pytest suite could not be executed here.
