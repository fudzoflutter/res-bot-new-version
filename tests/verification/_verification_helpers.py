"""Shared fakes and helpers for the ``tests/verification`` suite.

Deliberately NOT named ``test_*`` so pytest does not collect it.

Everything here is network-free: Telegram is replaced by :class:`FakeBot` and
``CallbackQuery.answer`` is patched to record instead of hitting the API.

The session bootstrap (``tests/conftest.py``) pins the test environment and
strips ``SUPABASE_DB_URL`` before any module imports ``app.config``, so these
helpers always see the test configuration and never touch production.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aiogram.types import CallbackQuery, User as AiogramUser  # noqa: E402

from app.config import settings  # noqa: E402

# Identities provided by tests/conftest.py.
OWNER = settings.admin_id  # 111111111 — ADMIN_ID (full rights)
ADMIN_ID = 222222222  # ADMIN_IDS       — non-owner admin
OUTSIDER = 999000111  # ordinary Telegram user (no role)


# ---------------------------------------------------------------------------
# Telegram WebApp initData signing
# ---------------------------------------------------------------------------
def sign_init_data(fields: dict[str, str], token: str | None = None) -> str:
    """Return a correctly signed ``initData`` query string for *fields*."""
    token = settings.bot_token if token is None else token
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    # Real Telegram sends a URL-ENCODED query string.
    return urlencode([*((k, fields[k]) for k in sorted(fields)), ("hash", digest)])


def make_init_data(
    user_id: int, *, auth_date: int | None = None, token: str | None = None
) -> str:
    """Signed initData for *user_id* (``auth_date`` defaults to now)."""
    if auth_date is None:
        auth_date = int(time.time())
    return sign_init_data(
        {
            "auth_date": str(auth_date),
            "user": json.dumps(
                {"id": user_id, "first_name": "Tester"}, separators=(",", ":")
            ),
        },
        token=token,
    )


# ---------------------------------------------------------------------------
# Callback / message fakes
# ---------------------------------------------------------------------------
class Holder:
    """Replaces ``cb.message`` — records edits without network."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[object] = []

    async def edit_text(self, text, **kwargs):  # noqa: ANN001
        self.texts.append(text)
        self.markups.append(kwargs.get("reply_markup"))

    async def answer(self, text, **kwargs):  # noqa: ANN001
        self.texts.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class FakeBot:
    """Network-free Bot: records sends; supports per-chat failure injection."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, int]] = []
        self.retry429: dict[int, int] = {}
        self.netfail: dict[int, int] = {}
        self.forbidden: set[int] = set()

    async def me(self):  # noqa: ANN201
        return SimpleNamespace(username="test_bot", id=1, first_name="Test")

    def _maybe_fail(self, chat_id: int) -> None:
        from aiogram.exceptions import (
            TelegramForbiddenError,
            TelegramNetworkError,
            TelegramRetryAfter,
        )

        if self.retry429.get(chat_id, 0) > 0:
            self.retry429[chat_id] -= 1
            raise TelegramRetryAfter(method="sendMessage", message="429", retry_after=0)
        if self.netfail.get(chat_id, 0) > 0:
            self.netfail[chat_id] -= 1
            raise TelegramNetworkError(method="sendMessage", message="network down")
        if chat_id in self.forbidden:
            raise TelegramForbiddenError(method="sendMessage", message="blocked")

    async def send_message(self, chat_id, text, **kwargs):  # noqa: ANN001
        self._maybe_fail(chat_id)
        self.sent.append(("message", chat_id))

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):  # noqa: ANN001
        self._maybe_fail(chat_id)
        self.sent.append(("photo", chat_id))

    async def send_video(self, chat_id, video, caption=None, **kwargs):  # noqa: ANN001
        self._maybe_fail(chat_id)
        self.sent.append(("video", chat_id))

    async def send_animation(self, chat_id, animation, caption=None, **kwargs):  # noqa: ANN001
        self._maybe_fail(chat_id)
        self.sent.append(("animation", chat_id))

    async def send_sticker(self, chat_id, sticker, **kwargs):  # noqa: ANN001
        self.sent.append(("sticker", chat_id))

    async def send_voice(self, chat_id, voice, caption=None, **kwargs):  # noqa: ANN001
        self.sent.append(("voice", chat_id))

    async def send_video_note(self, chat_id, video_note, **kwargs):  # noqa: ANN001
        self.sent.append(("video_note", chat_id))

    async def send_audio(self, chat_id, audio, caption=None, **kwargs):  # noqa: ANN001
        self.sent.append(("audio", chat_id))

    async def send_document(self, chat_id, document, caption=None, **kwargs):  # noqa: ANN001
        self.sent.append(("document", chat_id))

    async def get_file(self, file_id):  # noqa: ANN001
        raise RuntimeError("test: file size unknown")

    async def download(self, file_id, destination=None):  # noqa: ANN001
        raise RuntimeError("test: download not implemented")


def patch_callback_answer() -> None:
    """Replace ``CallbackQuery.answer`` with a recorder (no Telegram call)."""

    async def _answer(self, text=None, show_alert=False, **kwargs):  # noqa: ANN001
        self._answers = list(getattr(self, "_answers", [])) + [
            (text or "", bool(show_alert))
        ]

    CallbackQuery.answer = _answer  # type: ignore[method-assign]


def make_callback(user_id: int, data: str) -> CallbackQuery:
    patch_callback_answer()
    cb = CallbackQuery.model_construct(
        id="1",
        from_user=AiogramUser(id=user_id, is_bot=False, first_name="Tester"),
        chat_instance="1",
        data=data,
        message=Holder(),
    )
    object.__setattr__(cb, "_bot", FakeBot())
    cb._answers = []
    return cb


# ---------------------------------------------------------------------------
# Business-connection fakes (user flow)
# ---------------------------------------------------------------------------
class PChat:
    """Chat stand-in with the attributes the reporter reads."""

    def __init__(self, chat_id: int = 555001) -> None:
        self.id = chat_id
        self.type = "private"
        self.title = "Partner chat"
        self.first_name = "Partner"
        self.username = "partner"


_MEDIA_FIELDS = (
    "sticker",
    "animation",
    "photo",
    "video",
    "voice",
    "video_note",
    "document",
    "audio",
)


class IncomingMessage:
    """Minimal business ``Message`` (media attributes default to ``None``)."""

    def __init__(
        self,
        message_id: int,
        user_id: int,
        *,
        text: str | None = None,
        connection_id: str = "bc_flow_0001",
        chat: PChat | None = None,
        **media: object,
    ) -> None:
        self.message_id = message_id
        self.from_user = SimpleNamespace(
            id=user_id, username="partner", first_name="Partner",
            last_name=None, is_bot=False,
        )
        self.chat = chat or PChat()
        self.business_connection_id = connection_id
        self.text = text
        self.caption = None
        for name, value in media.items():
            setattr(self, name, value)
        self.answers: list[str] = []

    async def answer(self, text, **kwargs):  # noqa: ANN001
        self.answers.append(text)

    def __getattr__(self, name: str):  # noqa: ANN001
        if name in _MEDIA_FIELDS:
            return None
        raise AttributeError(name)


class Deleted:
    """Minimal ``BusinessMessagesDeleted``."""

    def __init__(
        self, message_ids, *, connection_id: str = "bc_flow_0001", chat: PChat | None = None
    ) -> None:
        self.message_ids = list(message_ids)
        self.chat = chat or PChat()
        self.business_connection_id = connection_id


class BusinessConn:
    """Minimal ``BusinessConnection``."""

    def __init__(self, conn_id: str, user_id: int, *, enabled: bool = True) -> None:
        self.id = conn_id
        self.user = SimpleNamespace(
            id=user_id, username=f"user{user_id}", first_name="User",
            last_name=None, is_bot=False,
        )
        self.is_enabled = enabled
        self.user_chat_id = user_id


def file_ref(file_id: str):  # noqa: ANN201
    """Stand-in for a Telegram media object (only ``file_id`` is used)."""
    return SimpleNamespace(file_id=file_id)


# ---------------------------------------------------------------------------
# HTTP request stand-in (calls aiohttp handlers directly, no socket)
# ---------------------------------------------------------------------------
class JsonRequest:
    """Minimal aiohttp Request: signed initData header + JSON body.

    Used by the Web Admin verification modules so the handlers can be driven
    without binding a port (the real-HTTP checks live in
    ``test_web_security``).
    """

    def __init__(
        self,
        body: dict | None = None,
        *,
        bot=None,  # noqa: ANN001
        user_id: int = OWNER,
        init_data: str | None = None,
    ) -> None:
        if init_data is None:
            init_data = make_init_data(user_id)
        self.headers = {"X-Telegram-Init-Data": init_data}
        self.query = SimpleNamespace(get=lambda *a, **k: None)
        self.app = {"bot": bot}
        self._body = body

    async def json(self) -> dict | None:
        return self._body
