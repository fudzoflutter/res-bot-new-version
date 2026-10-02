# res-bot — fix report

This package contains the original project with targeted production fixes applied.

## Fixed

- Admin add/update role normalization (`ADMIN`/`MODERATOR`/`VIEWER` -> server roles).
- Admin frontend role select now reflects lowercase backend roles correctly.
- User delete no longer crashes in `remove_ban_without_notification`; cleanup failures no longer falsely report that the already-completed DB deletion failed.
- Instance lock is fail-closed on DB uncertainty; duplicate startup exits non-zero for Railway/Docker.
- Viewer/admin tab bootstrap now activates the first permitted tab.
- Connection verify is hidden when the role lacks `connections.manage`.
- Retention setting (7/30/90/180 days) is exposed in the admin UI.
- Dashboard no longer serializes every connection on each realtime refresh.
- Realtime refresh updates only the currently visible tab.
- Added lightweight `/api/me` for role/permission bootstrap.
- WebApp credentials are accepted from `X-Telegram-Init-Data` only, not URL query parameters.
- Hold Mode no longer loses Telegram Business monitoring data: incoming/edit/delete state is still stored while user-facing reports are muted.
- Maintenance state keeps the last known value on DB failures and fails closed on the first unknown read.
- Ban-list startup is retried and fails closed instead of silently treating all users as unbanned.
- Runtime admins are included in connection notifications and admin checks.
- Broadcast excludes every admin role and paginates safely even when a page contains only admins.
- Event cache/DB rows are isolated by `business_connection_id` to avoid cross-business collisions.
- Legacy pre-migration events are still readable for the matching connection owner.
- Supabase/SQLite schema and migration paths include `business_connection_id`.
- SQLite -> Supabase import/fallback sync preserves `business_connection_id`.
- `broadcast_recipients` is included in the manual Supabase schema.

## Validation performed

- All Python files parsed successfully with `ast`.
- `python -m compileall` completed successfully.
- `node --check public/app.js` completed successfully.
- `git diff --check` reports no whitespace errors.
- Critical fix patterns were checked with a dedicated static validation script.
- The new SQLite event lookup/update SQL was executed against a temporary SQLite database, including two Business connections with colliding chat/message IDs.

## Full pytest note

The current analysis container does not have `aiogram`, `aiosqlite`, or `asyncpg`, and external package download is unavailable. Therefore the repository's full pytest suite could not be executed here. On a normal development machine run:

```bash
python -m pip install -r requirements.txt
python -m pytest -q
```

No production token or `.env` file was added to this package.

## Final admin / Supabase pass

- Fixed the reported legacy-Supabase startup failure `column "business_connection_id" does not exist` by running the `events` column migration before the general schema/index creation. The migration is idempotent and is also available at `migrations/001_business_connection_id.sql`.
- Admin / moderator / viewer assignment sends a private Telegram notification to the target user and synchronizes the admin menu button.
- Admin role changes send old/new role notifications; removing a runtime admin sends a removal notification and removes the admin menu button.
- Promoting a previously banned user to an admin role clears the stale ban state so the role and moderation lists cannot disagree.
- Ban/unban actions send private notifications; ban reason is included when provided.
- Hold Mode ON/OFF sends mandatory notifications to registered users. Monitoring/cache/database capture continues during Hold so edit/delete data is not lost; only normal user actions and user-facing reports are paused.
- New users are registered before the Hold check, so Users and Analytics continue to reflect real users during maintenance.
- Broadcast recipient counting/sending is paginated and excludes admins and banned users consistently; preview/test/send/retry controls remain separately permission-checked.
- Dynamic Delete User action is guarded against double-clicks and reports partial cleanup warnings instead of hiding a successful DB deletion.
- Static admin-panel audit confirmed 17 fixed HTML controls are wired in JavaScript and 20 expected backend API route/method combinations are registered.

### Validation limits

The complete runtime `pytest` suite could not be executed in this analysis container because `aiogram`, `aiosqlite`, and `asyncpg` are not installed and external dependency installation is unavailable. Python parsing/compile checks, JavaScript syntax checks, migration-order checks, API/control wiring checks, and targeted SQLite SQL checks passed. Run `python -m pip install -r requirements.txt && python -m pytest -q` in the normal project environment before a production rollout.
