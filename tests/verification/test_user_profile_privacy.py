"""Admin user profiles must never expose private conversation events."""
import asyncio
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from _verification_helpers import OWNER, make_init_data
from app import web as endpoints


def test_user_profile_does_not_fetch_or_return_messages(monkeypatch):
    monkeypatch.setattr(endpoints.db, 'get_user', AsyncMock(return_value={
        'user_id': 9911001, 'first_name': 'Test', 'username': 'test',
    }))
    monkeypatch.setattr(endpoints.db, 'user_stats', AsyncMock(return_value={'events_total': 1}))
    monkeypatch.setattr(endpoints.db, 'connections_for_user', AsyncMock(return_value=[]))
    search = AsyncMock(return_value=[{'details': 'PRIVATE_CONVERSATION_CONTENT'}])
    monkeypatch.setattr(endpoints.db, 'search_events', search)

    async def check():
        app = web.Application()
        app.router.add_get('/api/users/{user_id}', endpoints.api_user_detail)
        async with TestClient(TestServer(app)) as client:
            response = await client.get('/api/users/9911001', headers={
                'X-Telegram-Init-Data': make_init_data(OWNER),
            })
            assert response.status == 200
            body = await response.json()
            assert body['user_id'] == 9911001
            assert 'events' not in body
            assert 'PRIVATE_CONVERSATION_CONTENT' not in str(body)
            search.assert_not_called()

    asyncio.run(check())
