"""Admin panel endi chatda EMAS — Web Mini App (TMA) ichida ochiladi.

Tekshiriladi:
* /admin admin uchun FAQAT bitta web_app tugmasini yuboradi;
* oddiy foydalanuvchiga /admin hech narsa yubormaydi;
* eski chat-admin panel (inline menyu / callbacklar) endi mavjud emas.
"""

from __future__ import annotations

import asyncio
import importlib.util
from types import SimpleNamespace

OWNER = 111111111      # tests/conftest.py: ADMIN_ID
ADMIN = 222222222      # tests/conftest.py: ADMIN_IDS
STRANGER = 999999999


class _Msg:
    def __init__(self, user_id: int) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.sent: list[tuple[str, object]] = []

    async def answer(self, text, reply_markup=None, **kwargs):  # noqa: ANN001
        self.sent.append((text, reply_markup))


def _buttons(markup):  # noqa: ANN001
    return [b for row in markup.inline_keyboard for b in row]


def test_admin_menu_is_single_webapp_button() -> None:
    from app.keyboards import user_kb

    buttons = _buttons(user_kb.admin_panel_menu())
    assert len(buttons) == 1
    assert buttons[0].web_app is not None
    assert buttons[0].web_app.url.startswith("http")
    assert not buttons[0].callback_data


def test_webapp_url_env_override(monkeypatch) -> None:  # noqa: ANN001
    from app.keyboards import user_kb

    monkeypatch.setenv("WEBAPP_URL", "https://example.test/panel")
    assert user_kb.webapp_url() == "https://example.test/panel"
    monkeypatch.delenv("WEBAPP_URL")
    assert user_kb.webapp_url() == user_kb.DEFAULT_WEBAPP_URL


def test_admin_command_only_for_admins() -> None:
    from app.handlers import admin as admin_handler
    from app.services import admin_roles

    admin_roles.reset_for_tests()
    for uid in (OWNER, ADMIN):
        msg = _Msg(uid)
        asyncio.run(admin_handler.cmd_admin(msg))
        assert len(msg.sent) == 1
        assert _buttons(msg.sent[0][1])[0].web_app is not None

    stranger = _Msg(STRANGER)
    asyncio.run(admin_handler.cmd_admin(stranger))
    assert stranger.sent == [], "oddiy foydalanuvchiga panel ko'rsatilmasligi kerak"


def test_old_chat_panel_is_gone() -> None:
    for module in (
        "app.handlers.admin_panel",
        "app.keyboards.admin_kb",
        "app.services.access",
        "app.services.connection_state",
    ):
        assert importlib.util.find_spec(module) is None, module
