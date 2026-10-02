"""
Offline smoke test – Telegram tarmog'iga ulanmaydi.

Ishga tushirish (loyiha ildizidan):

    python tests/smoke_test.py

1-qism (klaviaturalar, matnlar, sozlamalar) — har doim ishlaydi.
2-qism (Supabase DB) — faqat SUPABASE_DB_URL berilganda:

    SUPABASE_DB_URL="postgresql://..." python tests/smoke_test.py
"""

from __future__ import annotations

import sys as _sys

# Windows konsolida emoji chop etish uchun (cp1252 UnicodeEncodeError bermasin).
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("ADMIN_ID", "111111111")
# env.txt haqiqiy kalitlarni o'z ichiga oladi — testlar uni o'qimasin.
os.environ.setdefault("CODEBUFF_SKIP_ENV_FILE", "1")

from app.keyboards import user_kb  # noqa: E402
from app.utils import texts  # noqa: E402


def part1_offline() -> None:
    """Klaviaturalar, matnlar va havola tozalovchi (tarmoqsiz)."""
    assert user_kb.back_to_menu() is not None

    connect_kb = user_kb.connect_menu("privatezeninbot")
    assert connect_kb.inline_keyboard[0][0].text == "⚙️ Sozlamalarni ochish"

    # Admin panel = bitta Web Mini App tugmasi (chat menyusi yo'q).
    admin_buttons = [b for row in user_kb.admin_panel_menu().inline_keyboard for b in row]
    assert len(admin_buttons) == 1 and admin_buttons[0].web_app is not None

    # Matnlar o'zbekcha va formatlash xatosiz yig'iladi.
    texts.STATS_TITLE.format(body=texts.STATS_BODY.format(
        mention="x", user_id=1,
        connection_line=texts.CONNECTED_LINE,
        total="0", edits="0", deletes="0",
    ))

    # DUPLICATE_POLLER is defined once (not twice)
    assert texts.DUPLICATE_POLLER.count("{count}") == 1, \
        "DUPLICATE_POLLER has wrong {count} occurrences"

    # Havola tozalovchi xizmat ishlaydi (kuzatuv parametri olib tashlanadi).
    from app.services.linkcleaner import clean_url

    assert clean_url("https://x.com/p?utm_source=a&id=1") == "https://x.com/p?id=1"

    # Admin tekshiruvi admin_roles orqali.
    from app.services.admin_roles import is_admin
    assert is_admin(111111111) is True
    assert is_admin(99999) is False

    print("PART 1 (offline) PASSED ✅")


async def part2_supabase() -> None:
    """DB tekshiruvlari — faqat haqiqiy SUPABASE_DB_URL bilan."""
    from app.database import db

    await db.init()
    try:
        # -- kirish ochiq (tasdiqlash/ban tizimi olib tashlandi) --------------
        await db.upsert_user(9001, "testuser", "Test", None)
        row = await db.get_user(9001)
        assert row["username"] == "testuser"
        assert await db.count_users() >= 1

        # -- events + connections -------------------------------------------------
        await db.add_event(9001, "edit", "a -> b", chat_id=55, message_id=10,
                           sender_id=9001)
        found = await db.get_event_by_message(55, 10)
        assert found and found["event_type"] == "edit"
        assert await db.count_user_events(9001, "edit") == 1

        await db.upsert_connection("bc_test", 9001, True, user_chat_id=9001)
        await db.upsert_connection("bc_test", 9001, False, user_chat_id=9001)
        conn = await db.get_connection("bc_test")
        assert not conn["is_enabled"] and conn["disconnected_at"]

        # -- activity log (new) --------------------------------------------------
        await db.add_activity_log("test_event", "test description",
                                  connection_id="bc_test", user_id=9001, severity="INFO")
        count = await db.count_activity_logs()
        assert count >= 1
        logs = await db.recent_activity_logs(limit=5)
        assert any(l["event_type"] == "test_event" for l in logs)

        # -- bot settings (new) --------------------------------------------------
        await db.set_setting("test_key", "test_value")
        val = await db.get_setting("test_key")
        assert val == "test_value"

        # -- all_connections (new) --------------------------------------------------
        conns = await db.all_connections()
        assert isinstance(conns, list)

        print("PART 2 (Supabase) PASSED ✅")
    finally:
        await db.close()


if __name__ == "__main__":
    part1_offline()
    if os.getenv("SUPABASE_DB_URL"):
        asyncio.run(part2_supabase())
    else:
        print("PART 2 SKIPPED (SUPABASE_DB_URL not set - Supabase tests skipped)")
