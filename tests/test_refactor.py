"""
Core functionality validation test (pytest-compatible).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Set up environment BEFORE any imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["CODEBUFF_SKIP_ENV_FILE"] = "1"
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("ADMIN_ID", "111111111")

import pytest


# ---------------------------------------------------------------------------
# KEYBOARD TESTS
# ---------------------------------------------------------------------------
class TestUserKeyboard:
    def test_main_menu_has_statistika(self):
        from app.keyboards import user_kb
        menu = user_kb.main_menu(connected=False)
        labels = [b.text for row in menu.inline_keyboard for b in row]
        assert any("Statistika" in l for l in labels)

    def test_main_menu_has_ulanish(self):
        from app.keyboards import user_kb
        menu = user_kb.main_menu(connected=False)
        labels = [b.text for row in menu.inline_keyboard for b in row]
        assert any("Ulanish" in l for l in labels)

    def test_main_menu_no_foydalanuvchilar(self):
        """User access system removed — no user management button."""
        from app.keyboards import user_kb
        menu = user_kb.main_menu(connected=True)
        labels = [b.text for row in menu.inline_keyboard for b in row]
        assert not any("Foydalanuvchilar" in l for l in labels), \
            "User management button must be gone"

    def test_main_menu_no_premium(self):
        from app.keyboards import user_kb
        menu = user_kb.main_menu(connected=True)
        labels = [b.text for row in menu.inline_keyboard for b in row]
        assert not any("Premium" in l for l in labels)

    def test_connected_menu_shows_checkmark(self):
        from app.keyboards import user_kb
        menu = user_kb.main_menu(connected=True)
        labels = [b.text for row in menu.inline_keyboard for b in row]
        assert any("Ulangan" in l for l in labels)

    def test_connect_menu_settings_button(self):
        from app.keyboards import user_kb
        kb = user_kb.connect_menu("testbot")
        assert kb.inline_keyboard[0][0].text == "⚙️ Sozlamalarni ochish"




# ---------------------------------------------------------------------------
# ACCESS SERVICE TESTS
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TEXT TESTS
# ---------------------------------------------------------------------------
class TestTexts:
    def test_no_duplicate_poller(self):
        """Duplicate DUPLICATE_POLLER definition fixed."""
        from app.utils import texts
        assert texts.DUPLICATE_POLLER.count("{count}") == 1

    def test_stats_body_no_users_field(self):
        """Regular users don't see global user count."""
        from app.utils import texts
        # new STATS_BODY doesn't have {users} placeholder
        assert "{users}" not in texts.STATS_BODY

    def test_welcome_text_exists(self):
        from app.utils import texts
        assert texts.WELCOME
        assert "<b>" in texts.WELCOME

    def test_business_connected_text(self):
        from app.utils import texts
        assert texts.BUSINESS_CONNECTED
        assert "ulangan" in texts.BUSINESS_CONNECTED.lower()

    def test_no_access_texts(self):
        """Verify access system texts are gone."""
        from app.utils import texts
        assert not hasattr(texts, "ACCESS_PENDING"), "ACCESS_PENDING must be removed"
        assert not hasattr(texts, "ACCESS_DENIED"), "ACCESS_DENIED must be removed"
        assert not hasattr(texts, "ACCESS_BANNED"), "ACCESS_BANNED must be removed"
        assert not hasattr(texts, "USER_APPROVED"), "USER_APPROVED must be removed"
        assert not hasattr(texts, "USERS_PANEL_TITLE"), "USERS_PANEL_TITLE must be removed"
        assert not hasattr(texts, "USERS_COUNT"), "USERS_COUNT must be removed"

    def test_format_texts(self):
        from app.utils import texts
        result = texts.STATS_TITLE.format(body=texts.STATS_BODY.format(
            mention="Test",
            user_id=123,
            connection_line=texts.CONNECTED_LINE,
            total="0",
            edits="0",
            deletes="0",
        ))
        assert "Test" in result
        assert "123" in result


# ---------------------------------------------------------------------------
# CONFIG TESTS
# ---------------------------------------------------------------------------
class TestConfig:
    def test_no_test_mode(self):
        """TEST_MODE removed from config."""
        from app.config import settings
        assert not hasattr(settings, "test_mode"), "test_mode must be removed from config"

    def test_admin_id_configured(self):
        from app.config import settings
        assert settings.admin_id == 111111111

    def test_bot_token_configured(self):
        from app.config import settings
        assert settings.bot_token == "123456:TEST-TOKEN"


# ---------------------------------------------------------------------------
# LINKCLEANER TEST
# ---------------------------------------------------------------------------
class TestLinkCleaner:
    def test_removes_utm_params(self):
        from app.services.linkcleaner import clean_url
        result = clean_url("https://x.com/p?utm_source=a&id=1")
        assert result == "https://x.com/p?id=1"

    def test_keeps_non_tracking_params(self):
        from app.services.linkcleaner import clean_url
        result = clean_url("https://example.com/page?id=1&name=test")
        assert "id=1" in result
        assert "name=test" in result


# ---------------------------------------------------------------------------
# REPORTER CACHE TESTS
# ---------------------------------------------------------------------------
class TestReporterCache:
    def test_remember_and_recall(self):
        from app.services.reporter import remember, recall, clear_instant_cache
        clear_instant_cache()
        remember(100, 200, "text", "hello world", sender_id=1)
        cached = recall(100, 200)
        assert cached is not None
        assert cached["details"] == "hello world"
        assert cached["event_type"] == "text"
        clear_instant_cache()

    def test_recall_miss_returns_none(self):
        from app.services.reporter import recall, clear_instant_cache
        clear_instant_cache()
        assert recall(999, 999) is None

    def test_clear_cache(self):
        from app.services.reporter import remember, recall, clear_instant_cache
        remember(111, 222, "photo", "img", sender_id=1)
        clear_instant_cache()
        assert recall(111, 222) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
