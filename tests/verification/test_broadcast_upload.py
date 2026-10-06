"""Local media test uploads over HTTP and reusable Telegram references."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from _verification_helpers import OWNER, make_init_data
from app import web as endpoints
from app.services import broadcast


@pytest.mark.parametrize('mime,kind', [('image/jpeg', 'photo'), ('video/mp4', 'video'), ('image/gif', 'animation')])
def test_local_upload_test_and_reuse(monkeypatch, mime, kind):
    monkeypatch.setattr(endpoints.audit, 'log_action', AsyncMock())
    async def check():
        paths = []
        async def send(target, **kwargs):
            assert target == OWNER
            media = kwargs[kind]
            paths.append(Path(media.path))
            assert b''.join([chunk async for chunk in media.read(None)]) == b'original-media'
            assert kwargs['caption'] == 'Sinov'
            assert kwargs['reply_markup'].inline_keyboard[0][0].text == 'Kanal'
            reference = SimpleNamespace(file_id='AgAC_reusable_media_12345')
            return SimpleNamespace(photo=[reference] if kind == 'photo' else [], **({kind: reference} if kind != 'photo' else {}))
        bot = SimpleNamespace(**{f'send_{kind}': AsyncMock(side_effect=send)})
        app = web.Application(client_max_size=52 * 1024 * 1024)
        app['bot'] = bot
        app.router.add_post('/test', endpoints.api_broadcast_test)
        async with TestClient(TestServer(app)) as client:
            def form():
                data = aiohttp.FormData()
                data.add_field('payload', json.dumps({'caption': 'Sinov', 'buttons': [{'text': 'Kanal', 'url': 'https://t.me/test'}]}))
                data.add_field('media', b'original-media', filename='media', content_type=mime)
                return data
            before = dict(broadcast.state)
            response = await client.post('/test', data=form())
            assert response.status == 401
            response = await client.post('/test', data=form(), headers={'X-Telegram-Init-Data': make_init_data(OWNER)})
            assert response.status == 200, await response.text()
            result = await response.json()
            assert result['target'] == OWNER
            assert result['media_file_id'] == 'AgAC_reusable_media_12345'
            assert broadcast.state == before
            assert paths and all(not path.exists() for path in paths)
            ad = broadcast.build_ad({'caption': 'Sinov', 'media_file_id': result['media_file_id'], 'media_type': kind})
            assert broadcast.build_ad(ad.to_dict()).media_url == ad.media_url
            reuse_bot = SimpleNamespace(**{f'send_{kind}': AsyncMock()})
            await broadcast.send_one(reuse_bot, OWNER, ad)
            assert getattr(reuse_bot, f'send_{kind}').await_args.kwargs[kind] == result['media_file_id']
    asyncio.run(check())


def test_media_errors_are_actionable():
    assert 'Mahalliy fayl' in endpoints._test_send_error(Exception('wrong type of the web page content'))
    assert '/start' in endpoints._test_send_error(Exception("bot can't initiate conversation"))


def test_invalid_upload_is_cleaned_without_sending(monkeypatch, tmp_path):
    original = endpoints.tempfile.NamedTemporaryFile
    monkeypatch.setattr(endpoints.tempfile, 'NamedTemporaryFile', lambda **kwargs: original(dir=tmp_path, **kwargs))
    async def check():
        bot = SimpleNamespace(send_photo=AsyncMock())
        app = web.Application()
        app['bot'] = bot
        app.router.add_post('/test', endpoints.api_broadcast_test)
        async with TestClient(TestServer(app)) as client:
            data = aiohttp.FormData()
            data.add_field('payload', json.dumps({'caption': 'x' * 1025}))
            data.add_field('media', b'original-media', filename='photo.jpg', content_type='image/jpeg')
            response = await client.post('/test', data=data, headers={'X-Telegram-Init-Data': make_init_data(OWNER)})
            assert response.status == 400
            bot.send_photo.assert_not_called()
            assert list(tmp_path.iterdir()) == []
    asyncio.run(check())
