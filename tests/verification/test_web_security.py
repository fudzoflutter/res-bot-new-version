"""Web Admin security verification over REAL HTTP.

Starts the actual aiohttp app on an ephemeral port and drives it with a real
client, so the HTTP status codes (401 vs 403) and header handling are verified
end to end — not just at the handler level.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from _verification_helpers import ADMIN_ID, FakeBot, OUTSIDER, OWNER, make_init_data

from app.database import db
from app.services import admin_roles
from app.web import start_web_server


async def _check() -> None:
    import aiohttp

    await db.init()
    await db.upsert_user(OWNER, "owner", "Owner", None)
    await admin_roles.load()

    os.environ["PORT"] = "0"  # let the OS pick a free port
    site = await start_web_server(FakeBot())
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    async def get(path: str, init_data: str | None = None):
        headers = {} if init_data is None else {"X-Telegram-Init-Data": init_data}
        async with aiohttp.ClientSession() as session:
            async with session.get(base + path, headers=headers) as resp:
                return resp.status, await resp.text()

    async def post(path: str, init_data: str, body: dict):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                base + path,
                headers={"X-Telegram-Init-Data": init_data},
                json=body,
            ) as resp:
                return resp.status, await resp.text()

    try:
        # --- unauthenticated -> 401 ---------------------------------------
        assert (await get("/api/stats", "test"))[0] == 401  # fake header
        assert (await get("/api/stats"))[0] == 401  # missing header
        assert (await get("/api/settings", make_init_data(OWNER).replace("hash=", "hash=0", 1)))[0] == 401
        assert (await get("/api/settings", make_init_data(OWNER, auth_date=int(time.time()) - 3 * 86400)))[0] == 401
        assert (await get("/api/settings", make_init_data(OWNER, token="999999:OTHER-TOKEN")))[0] == 401

        # --- authenticated but not an admin -> 403 -------------------------
        assert (await get("/api/settings", make_init_data(OUTSIDER)))[0] == 403

        # --- admin / owner ------------------------------------------------
        status, body = await get("/api/settings", make_init_data(ADMIN_ID))
        assert status == 200 and json.loads(body)["is_owner"] is False
        status, body = await get("/api/stats", make_init_data(OWNER))
        assert status == 200 and json.loads(body)["role"] == "owner"

        # --- owner-only write endpoint -------------------------------------
        assert (await post("/api/settings", make_init_data(ADMIN_ID), {"maintenance_mode": True}))[0] == 403
        # tampered client role/isOwner/isAdmin must be ignored server-side
        assert (
            await post(
                "/api/settings",
                make_init_data(ADMIN_ID),
                {"maintenance_mode": True, "role": "owner", "isOwner": True, "isAdmin": True},
            )
        )[0] == 403
        assert (
            await post(
                "/api/settings",
                make_init_data(OUTSIDER),
                {"maintenance_mode": True, "role": "owner", "isOwner": True},
            )
        )[0] == 403
        assert (await post("/api/settings", make_init_data(OWNER), {"maintenance_mode": True}))[0] == 200
        await post("/api/settings", make_init_data(OWNER), {"maintenance_mode": False})

        # --- broadcast status endpoint -------------------------------------
        assert (await get("/api/broadcast/status", "fake"))[0] == 401
        assert (await get("/api/broadcast/status", make_init_data(OUTSIDER)))[0] == 403
        assert (await get("/api/broadcast/status", make_init_data(ADMIN_ID)))[0] == 200
    finally:
        await site.stop()
        await db.close()


def test_web_admin_http_security() -> None:
    asyncio.run(_check())
