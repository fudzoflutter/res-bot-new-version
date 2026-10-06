# 👁 Telegram Business Monitor Bot

An **aiogram 3** bot that connects to users through *Telegram for Business*
and reports message activity: edits and deletions of their chat partners'
messages. Sent messages are cached silently and never forwarded; photos /
videos / GIFs / stickers / voice messages / circular videos are re-sent only
when deleted, and only to the **owner of that very connection**.

The bot is intentionally **lean** and **private**:

* regular bot users receive **no** business data at all (no connections,
  messages, deleted/edited lists, cache, database or activity logs);
* the admin panel is a **Telegram Web Mini App**, protected **server-side**
  (`ADMIN_ID` / `ADMIN_IDS` + verified `initData`), hiding buttons is not the security model;
* there is **no** subscriber/approval system (pending / allowed / denied /
  banned users were removed entirely);
* it also **cleans links** (strips tracking parameters) for anyone.

---

## ✨ Features

| # | Feature |
|---|---------|
| 1 | **Connect** button guides the user into *Telegram Settings → Business → Chatbots* |
| 2 | **Statistics** button — a user sees only their own numbers (messages cached, edits, deletions) |
| 3 | Instant notifications to the owner when a business connection is established or lost |
| 4 | Reports only for activity that matters: partner message **edits** (old → new) and **deletions** — cached media is re-sent when deleted. The owner's own actions are never reported |
| 5 | **Link cleaner** — send any link and get it back without tracking params (`utm_*`, `fbclid`, `gclid`, …) |
| 6 | **Strict connection isolation** — connection A's reports never reach owner B, other users, subscribers or admins |
| 7 | **Admin panel as a Web Mini App** — dashboard, connections, analytics, ad builder, moderation, settings (see below) |
| 8 | **Reliability** — DB retry with exponential backoff, in-memory message cache with TTL, duplicate-event protection, retention cleanup, watchdog with health status and throttled admin alerts |
| 9 | **Admin roles** — 👑 Owner (`ADMIN_ID`) and 🛡 Admins (`ADMIN_IDS` / added from the panel). Destructive actions are owner-only and every action is re-checked server-side |

Extras: colored inline buttons (`style=` — Bot API 9.4+), premium emoji icons
(`icon_custom_emoji_id`), anti-flood, Telegram API guard (429 `RetryAfter`,
backoff, timeouts, concurrency limit), Supabase storage with a local SQLite
fallback.

---

## 🚀 Setup

```bash
# 1. Python 3.11+ required
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Dependencies
pip install -r requirements.txt

# 3. Configuration
cp .env.example .env             # Windows: copy .env.example .env
#   -> put your BOT_TOKEN (from @BotFather) and your ADMIN_ID (from @userinfobot)
#   -> optionally ADMIN_IDS=111,222 and ADMIN_OWNER_IDS=333

# 4. Run
python run.py
```

> A second key file, `env.txt`, is also read (and wins over `.env`).  Either
> works; keep your real keys out of version control (both are gitignored).
> Never hardcode tokens/passwords in the source — the code only reads env.

### 🐘 Switching to Supabase (cloud database)

The bot runs fine on local SQLite, but with a free [Supabase](https://supabase.com)
project your data lives in the cloud (and survives machine reinstalls):

1. Supabase Dashboard → your project → green **Connect** button →
   **Connection pooling** → copy the URI
   (`postgresql://postgres.xxxx:PASSWORD@aws-0-region.pooler.supabase.com:6543/postgres`).
2. Put it in `env.txt` / `.env` as `SUPABASE_DB_URL=...`.
3. Restart the bot. Tables/indexes are created automatically and the existing
   `bot.db` rows are **imported once** (tracked in `supabase_migrations`).
4. Verify before/after switching with the integration suite:

   ```bash
   SUPABASE_DB_URL="postgresql://..." python tests/supabase_test.py
   ```

If Supabase is temporarily unreachable at startup, the bot retries with
backoff and then keeps running on **SQLite fallback** (degraded mode) with a
CRITICAL alert to the owners — it never dies silently, and the state is
visible in *🗄 Database* / *🤖 Bot Health*.

### Requirements for full functionality

* **Premium emoji** — every icon lives in `app/emoji_config.py`.
* **Premium emoji inside message text** needs one extra switch:
  `ENABLE_PREMIUM_EMOJI_TAGS = True`. It is off by default because Telegram
  rejects `<tg-emoji>` from a non-Premium bot (`DOCUMENT_INVALID`).
* **Business updates** — the connecting user needs a Telegram Business
  account (free features are enough to add a chatbot).
* In **@BotFather → /mybots → Bot Settings → Group/Business Privacy** make
  sure business access is allowed.

---

## 🗂 Project structure

```
bot.db                  # SQLite database (auto-created, fallback storage)
run.py                  # launcher: python run.py
logs/bot.log            # rotating runtime log (auto-created, 2 MB x 3 files)
app/
├── config.py           # ALL settings (.env / env.txt, admin roles, production flag)
├── database.py         # schema + every SQL query (facade + Postgres, retry/backoff)
├── storage_sqlite.py   # SQLite backend (fallback, identical API)
├── main.py             # wiring: bot, dispatcher, routers, alerts, watchdog, lock
├── middlewares.py      # registration + anti-flood + metrics
├── handlers/
│   ├── user.py         # /start menu, own statistics, connect guide
│   ├── admin.py        # /admin → Web Mini App button
│   └── business.py     # business connection notices + activity capture
├── keyboards/
│   └── user_kb.py      # user keyboards + admin Web Mini App button
├── services/
│   ├── admin_roles.py  # owner/admin roles (env + runtime, stored in bot_settings)
│   ├── alerts.py       # throttled admin alerts (cooldown, activity log)
│   ├── db_health.py    # DB health tracking + credential sanitising
│   ├── reporter.py     # builds reports, RAM cache (TTL), dedupe, media safety
│   ├── instance_lock.py# DB lock: only ONE copy may poll Telegram at a time
│   ├── duplicate_watch.py # turns silent 409 conflicts into an owner alert
│   ├── linkcleaner.py  # strips tracking params from links
│   └── watchdog.py     # retention cleanup + health monitoring + failure alerts
└── utils/
    ├── formatting.py   # HTML escaping, mentions, number formats
    ├── logger.py       # logging setup
    ├── telegram_api.py # 429/backoff/timeout/concurrency guard for Telegram calls
    ├── texts.py        # EVERY user-facing text (edit messages here)
    └── ui.py           # colored button factory
```

Tables: `users` (technical statistics only), `events` (the message cache that
lets deleted content be re-sent), `connections` (business connections),
`activity_log` (admin timeline), `bot_settings` (retention, maintenance,
roles, overrides) and `instance_lock` (which copy is polling). The old
`access` table is **no longer created**; an existing one is simply ignored.

### Indexes

`events`: `occurred_at`, `event_type`, `user_id`, `(chat_id, message_id)`,
`(event_type, occurred_at)`, `(user_id, occurred_at)` — the last two serve
retention cleanup, analytics and search. `activity_log`: `occurred_at`,
`event_type`, `connection_id`. `connections`: `user_id`.

---

## 🛡 Admin panel (Web Mini App)

The admin panel is **not** a chat menu any more — it opens as a Telegram
**Web Mini App (TMA)**.

How to open it (admins only):

* send `/start` or `/admin` → one button **🛡 Admin panelni ochish**;
* or tap the **Admin panel** menu button next to the message field (the bot
  sets it automatically for every admin at startup).

```
📊 Dashboard      live health / users / connections / deleted / edited / 24h activity
👥 Users          search / pagination / profile / Copy ID / Ban / Unban / Delete / direct message
📈 Analytics      24h / 7d / 30d / custom range, charts and top activity
🧾 Message log    deleted / edited / media events with pagination and user filtering
📢 Ad Builder     media + caption + buttons, preview, test send, broadcast, retry
🚫 Moderation     ban / unban users
👮 Admins         OWNER-only add / role change / remove
⚙️ Settings       Hold mode + retention + mandatory ON/OFF notifications
👤 Profile        current role, Telegram ID and effective permissions
```

The interface is mobile-first and uses a Telegram-style bottom navigation.
Secondary tools live under **Menyu**, while every permission is still enforced
by the backend.  Direct user messages require `users.message` and are limited
to OWNER/ADMIN.  The message journal reads the existing `events` table through
`GET /api/messages`; no duplicate message store is introduced.

Set `WEBAPP_URL` (public HTTPS URL of this deployment) in `.env` — see
`.env.example`.

Security: the buttons are only UX.  Every `/api/*` request is verified
server-side (Telegram `initData` HMAC + role + permission, see `app/web.py`);
a forged header never authenticates anyone.

### Roles

| Role | Can do |
|------|--------|
| 👑 **OWNER** (`ADMIN_ID`, `ADMIN_OWNER_IDS`) | everything, including admin management and owner-only settings |
| 🛡 **ADMIN** (`ADMIN_IDS`, added from the panel) | operational monitoring, users, moderation, analytics, broadcast and normal admin actions; OWNER-only actions are blocked |
| ⚖️ **MODERATOR** | users, ban/unban and message/moderation views |
| 👁 **VIEWER** | analytics/statistics read-only |

Runtime admins are stored in `bot_settings` (`admin_roles` key) and survive
restarts. Owners can only be changed through the environment — a runtime
owner cannot be created, which keeps the privilege boundary in your `.env`.

### Maintenance mode

When **ON**: normal user actions are paused, but Telegram Business monitoring
continues in the background. Incoming/edit/delete state is still cached/stored so
messages are not lost; only user-facing edit/delete reports are muted until Hold
Mode is turned off. Connection state stays synchronized with Telegram, the admin
panel and connection verification keep working, and every real ON/OFF transition
triggers a best-effort notification to every registered user.

### Retention

Choose 7 / 30 / 90 / 180 days in *⚙️ Settings*. The watchdog deletes older
`events` and `activity_log` rows on a schedule (15-minute ticks). Active
connections, the RAM cache and current business data are never removed.

---

## 🔐 Privacy model

```
Business Connection A → Owner A → partner's message → partner deletes it
    Owner A       ✅ receives the report
    Owner B       ❌ never
    Bot users     ❌ never
    Subscribers   ❌ (no such feature)
    Admins        ❌ not as a business report (they see only the admin panel)
```

* The owner's **own** messages are never reported (cached only, detected via
  `sender_id`).
* Admin messages are not reported either.
* A disabled or unknown connection produces no reports at all — each skip is
  logged with its reason.
* Duplicate delete/edit/connection events are suppressed (idempotency), so a
  redelivered update cannot produce a second report.

---

## ⚙️ Reliability

* **DB layer** — every query goes through one of four helpers that retry
  transient failures (connection reset, timeout, SQLite busy/locked) with
  exponential backoff (0.5 s → 1 s → 2 s, max 3 attempts) and rebuild the
  Postgres pool once on connection loss. Permanent errors (bad SQL, constraint
  violations) are **not** retried and are never swallowed: they are logged
  with a traceback, recorded in health and alerted (throttled).
* **Message cache** — a RAM cache (`app/services/reporter.py`) written without
  any `await`, so an instantly-deleted message is still reported. It has a
  size limit (5 000) and a TTL (6 h), expired entries are purged by the
  watchdog, and the database is the durable fallback (delete reports read the
  DB when the RAM entry is gone, e.g. after a restart).
* **Duplicate protection** — a 10-minute seen-cache keyed by
  (kind, connection, chat, message) suppresses redelivered incoming/edit/
  delete/connection events.
* **Large media** — files above 8 MB are streamed to a temporary file instead
  of RAM, sent, and deleted immediately; files above 100 MB are refused with a
  visible reason. All Telegram calls share a concurrency limit (4) and a
  timeout, and 429 `RetryAfter` is honoured with bounded retries.
* **Watchdog** — background service that pings the DB, runs retention
  cleanup, purges the cache and reports its own state in *Bot Health*
  (🟢 Running / ⚠️ WARN / 🔴 ERROR). Repeated failures trigger an admin alert
  and an activity-log entry; if it ever crashes, the supervisor restarts it
  and the owners are told.
* **Instance lock** — one copy polls Telegram at a time; see below. Lock
  acquire/release/loss events are written to the activity log.

### ⚠️ Only ONE copy may run at a time (409)

Telegram delivers updates of one bot token to **one** `getUpdates` connection
at a time. If a second copy is polling, the two copies **take turns**: aiogram
hides the `409 Conflict` inside its retry loop, so the bot *looks* healthy
while a random share of the updates goes to the other copy.

Two layers prevent that:

* **Single-instance lock** (`app/services/instance_lock.py`) — every copy
  writes a heartbeat into `instance_lock`; if another copy is alive (heartbeat
  younger than 60 s), the new copy does **not** start polling and reports the
  holder's host / PID / start time. A hard-killed process frees the lock once
  its heartbeat goes stale. `FORCE_POLL=1` disables the check (a warning is
  logged, shown in *Bot Health* and alerted to owners — use it only for
  emergency diagnostics, never in production).
  * **Redeploys / restarts are allowed** — the lock stores the platform's
    `RAILWAY_SERVICE_ID` / `RAILWAY_DEPLOYMENT_ID`, and a managed copy takes
    the lock over from its own service. The displaced copy notices on its next
    heartbeat (≤ 20 s) and stops polling, so updates never split. Another
    *service* is never touched.
* **409 watcher** (`app/services/duplicate_watch.py`) — a copy running *older*
  code cannot see the lock, so `aiogram.dispatcher` is monitored instead:
  three `409` errors within a minute send the owners a Telegram alert (with
  cooldown) and write an `activity_log` entry.

---

## 🧭 How it works

* **Business connection** — `/start` shows the menu; the **Connect** button
  walks the user through *Telegram Business → Chatbots*. The bot is notified
  the moment the connection is created or lost.
* **Reports** — while connected, every incoming business message is cached
  silently. The owner is only notified when a **partner** edits or deletes:
  * edit → `✏️ Xabar tahrirlandi` with 📱 Default (old) → 📲 Edited (new);
    both texts/captions are kept in the event row;
  * delete (text) → the full original text;
  * delete (media) → the cached file is re-sent (photo / video / GIF /
    sticker / voice / circular video). If Telegram refuses the original form,
    the file is streamed and re-sent as audio / video / document.
* **Link cleaner** — any message containing a link gets the tracking
  parameters stripped (only when something was actually removed).
* **Own statistics** — every user sees only their own cached/edited/deleted
  counters; global numbers live exclusively in the admin panel.

### 🔇 "Voice / circular video not coming back"

`VOICE_MESSAGES_FORBIDDEN` is the **recipient's** *Voice Messages* privacy
setting (it covers voice **and** circular video, and a bot is never a contact).
The bot sends voice notes in their original form. If Telegram rejects a voice
note because of recipient privacy settings, the bot sends an explanation instead
of disguising the recording as a `.bin` document. The recipient must allow voice
messages in *Settings → Privacy and Security → Voice Messages → Everybody* for
future deleted voice notes to arrive normally. Circular video retains its normal
video fallback.

### Performance

* Connections are cached in memory (`_connection_cache`) and invalidated on
  every `business_connection` event (5-minute TTL safety net).
* `all_connections_with_stats()` aggregates per-connection counters in **one**
  query, and sender names are cached — no N+1 lookups while rendering the
  panel.
* Lists are paginated with DB `LIMIT/OFFSET` (5 rows per page) — Telegram
  never receives huge messages.
* Health counters show the effect: DB retries, reconnects, Telegram 429/409
  counts, reporter counters and watchdog cleanup statistics.

---

## 📌 Release notes / known behaviour

* **Broadcast media** — faqat **Media URL** (http/https) qabul qilinadi; fayl
  yuklash yo'q. Rasm/video oldindan joylashtirilgan bo'lishi kerak.
* **Broadcast Retry** — «Retry Failed» faqat aniq `FAILED` va hali urinilmagan
  `PENDING` oluvchilarga ishlaydi; `SENT` hech qachon takrorlanmaydi. Restart yoki
  network holatida `SENDING` aniq bo'lmasa `UNKNOWN` bo'ladi va xavfsizlik uchun
  avtomatik qayta yuborilmaydi — Telegram so'rovni qabul qilgan bo'lishi mumkin.
* **Rate-limit** — oddiy foydalanuvchidan soniyasiga 5 tadan ortiq xabar (15 ta
  callback) tashlab yuboriladi; bu restartdan keyin ham amal qiladi. Business
  hodisalari va adminlar bundan mustasno.
* **Bot o'chiq paytdagi update'lar** — yo'qolmaydi: start_polling ularni
  handlerlarga yetkazadi (`/start`, business_connection, edited/deleted).
* **Vaqt** — baza, analytics va hisobotlar **Toshkent vaqti (UTC+5)** da.
  Server soati (Railway'da UTC) ishlatilmaydi. ⚠️ Eski (UTC) yozilgan
  qatorlar mavjud bo'lsa, ular yangilaridan ~5 soat orqada ko'rinadi.
* **Supabase'ga qaytish** — SQLite fallbackdan keyin avtomatik emas: restart
  kerak. Restartda fallback ma'lumotlari yangilik bo'yicha (timestamp) sinxronlanadi.

## 🧪 Tests

```bash
python -m pytest tests/ -q              # unit tests (offline)
python tests/monitoring_test.py         # cache, isolation, dedupe, roles,
                                        # maintenance, retention,
                                        # DB/Telegram resilience, media safety,
python tests/reporter_test.py           # report rules (what is reported)
python tests/duplicate_test.py          # instance lock + 409 watcher
python tests/features_test.py           # connections, counters, wiring
python tests/fixes_test.py              # cache/DB regressions, schema parity
python tests/smoke_test.py              # keyboards/texts (+ Supabase if URL set)
```

All suites run **offline** (no Telegram network, no real database) unless
`SUPABASE_DB_URL` is provided for `tests/supabase_test.py`.

---

## 🛠 Customization cheat-sheet

| I want to change… | Go to |
|---|---|
| Bot texts / wording / language | `app/utils/texts.py` |
| Premium emoji icons | `app/emoji_config.py` — single registry per screen |
| Button colors | `style=` args in `app/keyboards/*.py` (`danger`/`success`/`primary`) |
| "Online" window | `ONLINE_WINDOW_SECONDS` in `app/utils/timeutils.py` |
| Admin roles | `ADMIN_ID` / `ADMIN_IDS` / `ADMIN_OWNER_IDS` in `env.txt`; panel: ⚙️ Settings → 👑 Admins |
| Running two copies at once / 409 errors | `FORCE_POLL=1` (emergency only — see above) |
| Link cleaning rules | `TRACKING_EXACT` / `TRACKING_PREFIXES` in `app/services/linkcleaner.py` |
| Report times (timezone) | `REPORT_UTC_OFFSET_HOURS` in `app/utils/timeutils.py` |
| Report text limits | `MAX_TEXT` in `app/services/reporter.py` |
| Report rules (what gets reported) | `report_incoming` / `report_edited` / `report_deleted` in `app/services/reporter.py` |
| Cache size / TTL | `INSTANT_CACHE_MAX` / `INSTANT_CACHE_TTL_SECONDS` in `app/services/reporter.py` |
| Media size limits | `MEDIA_MEMORY_LIMIT` / `MEDIA_MAX_DOWNLOAD` in `app/services/reporter.py` |
| Retry/backoff policy | `RETRY_ATTEMPTS` / `RETRY_BASE_DELAY` in `app/database.py`; `app/utils/telegram_api.py` |
| Retention choices | `⚙️ Settings` in the panel (stored in `bot_settings`) |
