import asyncio
from unittest.mock import AsyncMock

from app.services.reporter import EVENT_VOICE, Reporter


def test_voice_is_sent_in_original_form():
    bot = AsyncMock()
    result = asyncio.run(Reporter(bot)._resend_media(
        123, EVENT_VOICE, "voice-id", header="Deleted", chat_title="Chat",
    ))
    assert result == (True, None)
    assert bot.send_voice.await_args.kwargs["voice"] == "voice-id"
    bot.send_document.assert_not_called()
    bot.download.assert_not_called()


def test_voice_privacy_does_not_send_binary_document():
    bot = AsyncMock()
    bot.send_voice.side_effect = RuntimeError("VOICE_MESSAGES_FORBIDDEN")
    result = asyncio.run(Reporter(bot)._resend_media(
        123, EVENT_VOICE, "voice-id", header="Deleted", chat_title="Chat",
    ))
    assert result == (False, "VOICE_MESSAGES_FORBIDDEN")
    bot.send_document.assert_not_called()
    bot.download.assert_not_called()


def test_downloaded_voice_remains_voice():
    bot = AsyncMock()
    asyncio.run(Reporter(bot)._send_as_file(
        123, EVENT_VOICE, "Deleted", data=b"OggS", file_name="voice.bin",
    ))
    voice = bot.send_voice.await_args.kwargs["voice"]
    assert voice.filename == "voice.ogg"
    bot.send_audio.assert_not_called()
    bot.send_document.assert_not_called()
