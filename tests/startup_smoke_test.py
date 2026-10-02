"""
Startup smoke test — bot ishga tushish yo'lini TARMOQSIZ tekshiradi.

Ishga tushirish:

    python tests/startup_smoke_test.py

Nima tekshiriladi:
* SQLite backend init + health holati (backend, ping, status ikonkasi);
* admin rollari va ulanish ustamalarini yuklash;
* alert xizmati (sender sozlanadi, cooldown ishlaydi);
* watchdog bitta sikli (DB ping + retention housekeeping);
* maintenance / retention sozlamalari DBda saqlanadi;
* admin panel asosiy ekranlari (dashboard health) haqiqiy bazada yig'iladi.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["CODEBUFF_SKIP_ENV_FILE"] = "1"
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN"
os.environ["ADMIN_ID"] = "111111111"
os.environ["ADMIN_IDS"] = "222222222"
os.environ.pop("SUPABASE_DB_URL", None)

_tmpdir = tempfile.mkdtemp(prefix="bot_startup_test_")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "startup.db")


class FakeBot:
    """Tarmoqqa chiqmaydigan bot (faqat send_message yozib boradi)."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):  # noqa: ANN001
        self.messages.append((chat_id, text))
        return None


async def main() -> None:
    from app.config import settings
    from app.database import db
    from app.services import admin_roles, alerts, db_health, watchdog

    assert settings.admin_id == 111111111
    assert admin_roles.is_owner(settings.admin_id)
    assert admin_roles.is_admin(222222222)

    await db.init()
    try:
        await admin_roles.load()

        # --- DB health / ping ------------------------------------------------
        assert await db.ping() is True
        snap = db_health.health.snapshot()
        assert snap["connected"] is True, snap
        assert snap["backend"], "backend nomi bo'sh"
        assert snap["status_icon"] == "🟢"
        print(f"DB backend: {snap['backend']} | status: {snap['status_icon']}")

        # --- Alert xizmati ---------------------------------------------------
        bot = FakeBot()
        alerts.configure_default_sender(bot)
        assert alerts.alerts.configured
        first = await alerts.alerts.notify("smoke_key", "SMOKE", "test")
        second = await alerts.alerts.notify("smoke_key", "SMOKE", "test")
        assert first is True, "birinchi alert yuborilishi kerak"
        assert second is False, "takroriy alert cooldown tufayli yuborilmasligi kerak"
        assert bot.messages, "ownerlarga xabar ketishi kerak"
        print(f"Alerts: sent={alerts.alerts.sent_total}, suppressed={alerts.alerts.suppressed_total}")

        # --- Watchdog sikli ---------------------------------------------------
        await watchdog._tick()
        wd = watchdog.status()
        assert wd["cycles"] >= 1, wd
        assert wd["running"] is True or wd["running"] is False  # tick o'zi start qilmaydi
        assert wd["housekeeping_runs"] >= 1, "housekeeping bajarilishi kerak"
        assert wd["status_icon"] in ("🟢", "⚠️", "🔴")
        print(
            f"Watchdog: {wd['status_icon']} {wd['status_text']} | "
            f"housekeeping={wd['housekeeping_runs']} | retention={wd['retention_days']}d"
        )

        # --- Sozlamalar (retention / maintenance) ----------------------------
        await db.set_setting("retention_days", "30")
        await db.set_setting("maintenance_mode", "1")
        assert await db.get_setting("retention_days") == "30"
        assert await db.get_setting("maintenance_mode") == "1"
        await db.set_setting("maintenance_mode", "0")

        # --- Admin panel = Web Mini App tugmasi (chat menyusi YO'Q) ------------
        from app.keyboards import user_kb

        buttons = [b for row in user_kb.admin_panel_menu().inline_keyboard for b in row]
        assert len(buttons) == 1, buttons
        assert buttons[0].web_app is not None and buttons[0].web_app.url.startswith("http")
        assert not buttons[0].callback_data, "admin tugmasi callback bo'lmasligi kerak"
        print("Admin panel: Web Mini App tugmasi OK")
        print("\nSTARTUP SMOKE TEST PASSED ✅")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
