"""Real Telegram Business Connection reconciliation."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from app.database import db
from app.utils.telegram_api import call as tg_call

logger = logging.getLogger(__name__)

_BOT: Optional[Any] = None


def configure_bot(bot: Any) -> None:
    global _BOT
    _BOT = bot


def reset_for_tests() -> None:
    global _BOT
    _BOT = None


def _looks_missing(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "business connection not found",
            "business connection not exist",
            "not found",
            "does not exist",
        )
    )


async def verify_connection(
    connection_id: str, *, force: bool = False, notify: bool = True
) -> dict[str, Any]:
    """Verify one DB connection against Telegram's authoritative state."""
    row = await db.get_connection(connection_id)
    if not row:
        return {"status": "missing_db", "changed": False, "connection_id": connection_id}
    if not row.get("is_enabled") and not force:
        return {"status": "skipped_disabled", "changed": False, "connection_id": connection_id}
    if _BOT is None:
        return {"status": "bot_unavailable", "changed": False, "connection_id": connection_id}

    try:
        remote = await tg_call(
            "get_business_connection",
            lambda: _BOT.get_business_connection(business_connection_id=connection_id),
            attempts=2,
        )
    except Exception as exc:
        if not _looks_missing(exc):
            # Network/5xx/permission-ish uncertainty is NOT enough to declare a disconnect.
            logger.info("Connection verification uncertain (%s): %s", connection_id, exc)
            return {
                "status": "unknown",
                "changed": False,
                "connection_id": connection_id,
                "error": type(exc).__name__,
            }
        remote = None

    remote_enabled = bool(remote and getattr(remote, "is_enabled", False))
    current_enabled = bool(row.get("is_enabled"))

    if remote is not None:
        owner = getattr(remote, "user", None)
        if owner is not None:
            try:
                await db.upsert_user(
                    int(owner.id),
                    getattr(owner, "username", None),
                    getattr(owner, "first_name", None),
                    getattr(owner, "last_name", None),
                )
            except Exception:
                logger.debug("Could not refresh owner profile during verification", exc_info=True)
        user_chat_id = getattr(remote, "user_chat_id", None) or row.get("user_chat_id")
    else:
        user_chat_id = row.get("user_chat_id")

    if remote_enabled == current_enabled:
        return {
            "status": "ok",
            "changed": False,
            "connection_id": connection_id,
            "is_enabled": remote_enabled,
        }

    owner_id = int(row.get("user_id") or 0)
    await db.upsert_connection(connection_id, owner_id, remote_enabled, user_chat_id)

    # Invalidate the in-process routing cache immediately.
    try:
        from app.services import reporter
        reporter.invalidate_connection(connection_id)
    except Exception:
        logger.debug("Reporter cache invalidation failed", exc_info=True)

    try:
        await db.add_activity_log(
            "connection_reconciled",
            f"Telegram state reconciled: {current_enabled} -> {remote_enabled}",
            connection_id=connection_id,
            user_id=owner_id,
            severity="WARNING",
        )
    except Exception:
        logger.debug("Connection reconcile log failed", exc_info=True)

    if notify and owner_id > 0:
        try:
            from app.utils import texts
            notice = texts.BUSINESS_ENABLED_AGAIN if remote_enabled else texts.BUSINESS_DISCONNECTED
            await tg_call(
                "connection_reconcile_notify",
                lambda: _BOT.send_message(owner_id, notice, parse_mode="HTML"),
                attempts=2,
            )
        except Exception:
            logger.info("Connection reconcile notification failed (user=%s)", owner_id, exc_info=True)

    return {
        "status": "changed",
        "changed": True,
        "connection_id": connection_id,
        "user_id": owner_id,
        "is_enabled": remote_enabled,
    }


async def verify_all(*, force: bool = False, notify: bool = True) -> dict[str, int]:
    """Reconcile all currently-enabled DB connections with Telegram."""
    rows = await db.all_connections()
    rows = [r for r in rows if r.get("is_enabled") or force]
    counts = {"checked": 0, "changed": 0, "ok": 0, "unknown": 0, "errors": 0}
    sem = asyncio.Semaphore(4)

    async def one(row: dict) -> None:
        async with sem:
            counts["checked"] += 1
            try:
                result = await verify_connection(
                    str(row.get("business_connection_id") or ""),
                    force=force,
                    notify=notify,
                )
                status = result.get("status")
                if result.get("changed"):
                    counts["changed"] += 1
                elif status == "ok":
                    counts["ok"] += 1
                elif status == "unknown":
                    counts["unknown"] += 1
                elif status not in ("missing_db", "skipped_disabled"):
                    counts["errors"] += 1
            except Exception:
                counts["errors"] += 1
                logger.exception("Connection verification failed")

    # Avoid scheduling unbounded Python task fan-out; semaphore controls API concurrency.
    for i in range(0, len(rows), 50):
        await asyncio.gather(*(one(row) for row in rows[i:i+50]))
    return counts
