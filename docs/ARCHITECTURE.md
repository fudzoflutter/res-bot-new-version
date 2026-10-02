# Architecture & refactor plan

This document records the **current** layout and the **target** structure for
the next structural refactor.  Nothing was moved in this pass — only safe,
verified cleanups were applied (see "Change policy").

## Change policy for the refactor

1. No behaviour changes while reorganising — move code, keep the API identical.
2. One area per pull request (admin, user, emoji, database, web, services).
3. `pytest -q` must stay green after every step.
4. Tests must never touch the production database (see `tests/conftest.py`).

## Current layout

```
app/
  config.py            settings (env snapshot), role_of()
  database.py          Database facade + PostgresDatabase + shared SQL helpers/constants
  storage_sqlite.py    SqliteDatabase backend
  emoji_config.py      EMOJI registry (premium id + fallback per entry)
  main.py              wiring: dispatcher, middlewares, web server, background tasks
  middlewares.py       RegisterUserMiddleware (registration + rate limit)
  web.py               Web Admin: initData auth + aiohttp routes + broadcast
  handlers/
    admin.py           /admin -> Web Mini App (TMA) button
    business.py        business connection / message / edited / deleted
    user.py            /start, statistics, connect, back-to-menu
  keyboards/
    user_kb.py         user keyboards + admin Web Mini App button
  services/            admin_roles, alerts, db_health, metrics,
                       duplicate_watch, instance_lock, linkcleaner, reporter, watchdog
  utils/               formatting, logger, tasks, telegram_api, texts, timeutils, ui
public/                TMA frontend (index.html, app.js, style.css)
tests/
  conftest.py                       pytest bootstrap (pins env, strips Supabase URL)
  <*_test.py>                       standalone suites (smoke/fixes/features/...)
  test_refactor.py                  pytest suite
  verification/                     production-verification suite
    _verification_helpers.py        shared network-free fakes
    test_supabase_readonly.py       opt-in, read-only Supabase check
    test_admin_webapp.py            /admin -> Web Mini App button, old chat panel gone
    test_user_flow.py               /start, messages, media, reconnect, errors
    test_web_security.py            real-HTTP 401/403 + tamper checks
    test_broadcast.py               retry semantics + 202/409 + counts
```

## Target layout (next refactor)

| Area     | Target                                                              | Source today                          |
|----------|---------------------------------------------------------------------|---------------------------------------|
| ADMIN    | Web Mini App only (`public/` + `app/web.py`)                        | `handlers/admin.py` (entry button)    |
| USER     | `app/handlers/user/` + `app/keyboards/user/`                        | `handlers/user.py`, `keyboards/user_kb.py` |
| EMOJI    | all literals from `app/emoji_config.py` (add missing entries)       | raw emoji in `handlers/`, `keyboards/` |
| DATABASE | `app/db/{facade,postgres,sqlite,sql}.py`                            | `database.py`, `storage_sqlite.py`    |
| WEB      | `app/web/{auth,routes,broadcast,server}.py`                         | `web.py`                              |
| SERVICES | business logic out of handlers into `app/services/`                 | `handlers/*`                          |

### Notes per area

* **Admin / User** — the callback constants (`CB_*`) already live next to
  their keyboards, so the split is a file move plus import updates.  
* **Emoji** — `app/emoji_config.py` is the single registry and is already used
  by `utils/ui.py` and `utils/texts.py`.  Some keyboards still
  embed raw literals; add entries there and replace them incrementally.
  Because emoji appear in message text (and tests assert on labels), this must
  be done screen-by-screen with tests green after each screen.
* **Database** — `Database` is the facade; `PostgresDatabase` and
  `SqliteDatabase` must keep the **same public method set**.  A parity check
  guards this: every public async method on `SqliteDatabase` must also exist on
  `PostgresDatabase`.
* **Web** — keep auth (initData validation + role check), routes, broadcast
  and server bootstrap as separate modules; `authorize()` is the single
  auth entry point for every protected endpoint.
* **Services** — handlers should orchestrate, not contain data logic.  The
  reporter/watchdog/instance_lock modules are the model to follow.

## Data-flow invariants (do not break)

* An ADMIN may read every panel screen; only the OWNER may perform
  destructive actions (verified in `tests/verification/test_admin_matrix.py`).
* The Web Admin derives its role **server-side** from initData; a valid
  non-admin gets `403`, an invalid/expired signature gets `401`
  (`tests/verification/test_web_security.py`).
* Broadcast is one-at-a-time: first request `202`, duplicate `409`; the owner
  is never a recipient, so `sent + failed == total`
  (`tests/verification/test_broadcast.py`).
* Reports go **only** to the connection owner.
