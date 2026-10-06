import asyncio
import io
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPhoto, SendVideoNote

from app.handlers import business
from app.emoji_config import EmojiEntry


def trigger(kind, sender=111):
    target = SimpleNamespace(
        message_id=8, photo=None, video=None, video_note=None,
    )
    media = SimpleNamespace(file_id="stored-media")
    setattr(target, kind, [media] if kind == "photo" else media)
    return SimpleNamespace(
        text="?", reply_to_message=target, business_connection_id="connection-a",
        from_user=SimpleNamespace(id=sender), chat=SimpleNamespace(id=555),
        message_id=9,
    )


def setup_db(monkeypatch, *, enabled=True, saved=""):
    database = SimpleNamespace(
        get_connection=AsyncMock(return_value={
            "user_id": 111, "user_chat_id": 222, "is_enabled": enabled,
        }),
        get_setting=AsyncMock(return_value=saved), set_setting=AsyncMock(),
    )
    monkeypatch.setattr(business, "db", database)
    return database


@pytest.mark.parametrize("kind", ["photo", "video", "video_note"])
def test_view_once_reuses_telegram_file_without_download(monkeypatch, kind):
    database = setup_db(monkeypatch)
    bot = AsyncMock()
    bot.send_video_note.return_value = SimpleNamespace(message_id=123)
    assert asyncio.run(business._save_replied_view_once(trigger(kind), bot))
    send = getattr(bot, "send_" + kind)
    assert send.await_args.args == (222, "stored-media")
    assert "business_connection_id" not in send.await_args.kwargs
    bot.get_file.assert_not_called()
    bot.download_file.assert_not_called()
    if kind == "video_note":
        bot.send_message.assert_awaited_once()
        assert "Aylana video saqlandi" in bot.send_message.await_args.args[1]
        assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"
    else:
        bot.send_message.assert_not_called()
        expected = "Rasm saqlandi" if kind == "photo" else "Video saqlandi"
        assert expected in send.await_args.kwargs["caption"]
        assert send.await_args.kwargs["parse_mode"] == "HTML"
    database.set_setting.assert_awaited_once()


@pytest.mark.parametrize("sender,enabled,saved", [
    (999, True, ""), (111, False, ""), (111, True, "1"),
])
def test_view_once_rejects_outsider_disabled_and_duplicate(monkeypatch, sender, enabled, saved):
    database = setup_db(monkeypatch, enabled=enabled, saved=saved)
    bot = AsyncMock()
    asyncio.run(business._save_replied_view_once(trigger("photo", sender), bot))
    bot.send_photo.assert_not_called()
    bot.get_file.assert_not_called()
    database.set_setting.assert_not_called()


@pytest.mark.parametrize("reason", [
    "wrong file identifier",
    "can't use file of type SelfDestructingPhoto as Photo",
])
def test_invalid_reference_uses_upload_fallback(monkeypatch, reason):
    database = setup_db(monkeypatch)
    bot = AsyncMock()
    bot.send_photo.side_effect = [TelegramBadRequest(
        method=SendPhoto(chat_id=222, photo="stored-media"),
        message=reason,
    ), None]
    bot.get_file.return_value = SimpleNamespace(file_path="photos/photo.jpg")
    bot.download_file.return_value = io.BytesIO(b"photo-content")
    asyncio.run(business._save_replied_view_once(trigger("photo"), bot))
    assert bot.send_photo.await_count == 2
    assert bot.send_photo.await_args.args[1].data == b"photo-content"
    database.set_setting.assert_awaited_once()


def test_privacy_rejection_does_not_download_or_mark_saved(monkeypatch):
    database = setup_db(monkeypatch)
    bot = AsyncMock()
    bot.send_photo.side_effect = TelegramBadRequest(
        method=SendPhoto(chat_id=222, photo="stored-media"),
        message="protected content",
    )
    asyncio.run(business._save_replied_view_once(trigger("photo"), bot))
    bot.download_file.assert_not_called()
    database.set_setting.assert_not_called()


def test_custom_emoji_is_rendered_in_saved_caption(monkeypatch):
    setup_db(monkeypatch)
    monkeypatch.setattr(business, "EMOJI", replace(
        business.EMOJI, view_once_photo=EmojiEntry("123456789", "🖼"),
    ))
    bot = AsyncMock()
    asyncio.run(business._save_replied_view_once(trigger("photo"), bot))
    caption = bot.send_photo.await_args.kwargs["caption"]
    assert '<tg-emoji emoji-id="123456789">' in caption
    assert "Rasm saqlandi" in caption


def test_receipt_failure_does_not_resend_saved_video(monkeypatch):
    database = setup_db(monkeypatch)
    bot = AsyncMock()
    bot.send_video_note.return_value = SimpleNamespace(message_id=123)
    bot.send_message.side_effect = RuntimeError("receipt failed")
    asyncio.run(business._save_replied_view_once(trigger("video_note"), bot))
    bot.send_video_note.assert_awaited_once()
    database.set_setting.assert_awaited_once()


@pytest.mark.parametrize("owner_role,sender_role", [
    ("owner", "admin"), ("admin", "owner"), ("admin", "admin"),
])
@pytest.mark.parametrize("kind", ["photo", "video", "video_note"])
def test_admins_save_each_others_media_to_requester_chat(monkeypatch, owner_role, sender_role, kind):
    setup_db(monkeypatch)
    monkeypatch.setattr(business.admin_roles, "role_of", lambda uid: {
        111: owner_role, 999: sender_role,
    }.get(uid))
    bot = AsyncMock()
    bot.send_video_note.return_value = SimpleNamespace(message_id=123)
    asyncio.run(business._save_replied_view_once(trigger(kind, sender=999), bot))
    assert getattr(bot, "send_" + kind).await_args.args == (999, "stored-media")
    if kind == "video_note":
        assert bot.send_message.await_args.args[0] == 999


@pytest.mark.parametrize("owner_role,sender_role", [
    (None, "admin"), ("admin", None), ("admin", "viewer"),
    ("admin", "moderator"), ("viewer", "admin"),
])
def test_cross_saving_requires_two_full_admins(monkeypatch, owner_role, sender_role):
    database = setup_db(monkeypatch)
    monkeypatch.setattr(business.admin_roles, "role_of", lambda uid: {
        111: owner_role, 999: sender_role,
    }.get(uid))
    bot = AsyncMock()
    asyncio.run(business._save_replied_view_once(trigger("photo", sender=999), bot))
    bot.send_photo.assert_not_called()
    database.set_setting.assert_not_called()


def test_expired_video_note_with_privacy_rejection_becomes_normal_video(monkeypatch):
    database = setup_db(monkeypatch)
    bot = AsyncMock()
    method = SendVideoNote(chat_id=222, video_note="stored-media")
    bot.send_video_note.side_effect = [
        TelegramBadRequest(method=method, message="FILE_REFERENCE_EXPIRED"),
        TelegramBadRequest(method=method, message="VOICE_MESSAGES_FORBIDDEN"),
    ]
    bot.get_file.return_value = SimpleNamespace(file_path="video/note.mp4")
    bot.download_file.return_value = io.BytesIO(b"original-video-with-audio")
    asyncio.run(business._save_replied_view_once(trigger("video_note"), bot))
    bot.send_video.assert_awaited_once()
    uploaded = bot.send_video.await_args.args[1]
    assert uploaded.data == b"original-video-with-audio"
    assert uploaded.filename.endswith(".mp4")
    assert "Aylana video saqlandi" in bot.send_video.await_args.kwargs["caption"]
    bot.send_document.assert_not_called()
    bot.send_message.assert_not_called()
    database.set_setting.assert_awaited_once()
