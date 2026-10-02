"""Premium obuna va qo'lda karta-to'lov oqimi.

Rejimlar:
* ``subscription_enabled = 0`` — obuna UI yashirin, View Once bepul.
* ``subscription_enabled = 1`` — obuna UI ko'rinadi, View Once faqat ACTIVE obunada.

To'lov provayderi yo'q: user karta orqali to'laydi, chek yuboradi, admin Web
paneldan tasdiqlaydi.  Narx/karta/muddat ``bot_settings``da, user obunasi va
cheklar alohida jadvallarda saqlanadi.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from app.database import db
from app.utils.timeutils import local_now

KEY_ENABLED = "subscription_enabled"
KEY_PRICE = "subscription_price"
KEY_DAYS = "subscription_days"
KEY_CARD_NUMBER = "subscription_card_number"
KEY_CARD_HOLDER = "subscription_card_holder"

DEFAULT_PRICE = 25_000
DEFAULT_DAYS = 30
CACHE_TTL = 30.0


@dataclass(frozen=True)
class SubscriptionConfig:
    enabled: bool
    price: int
    days: int
    card_number: str
    card_holder: str


_config_cache: tuple[float, Optional[SubscriptionConfig]] = (0.0, None)
_status_cache: dict[int, tuple[float, bool, dict[str, Any]]] = {}


def invalidate(user_id: Optional[int] = None) -> None:
    """Config/user obuna keshini bekor qiladi."""
    global _config_cache
    if user_id is None:
        _config_cache = (0.0, None)
        _status_cache.clear()
    else:
        _status_cache.pop(int(user_id), None)


def _int(raw: Any, default: int, *, minimum: int = 1, maximum: int = 10**9) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


async def get_config(*, fresh: bool = False) -> SubscriptionConfig:
    global _config_cache
    now = time.monotonic()
    if not fresh and _config_cache[1] is not None and now - _config_cache[0] < CACHE_TTL:
        return _config_cache[1]

    enabled = (await db.get_setting(KEY_ENABLED, "0")) == "1"
    price = _int(await db.get_setting(KEY_PRICE, str(DEFAULT_PRICE)), DEFAULT_PRICE)
    days = _int(await db.get_setting(KEY_DAYS, str(DEFAULT_DAYS)), DEFAULT_DAYS, maximum=3650)
    card_number = (await db.get_setting(KEY_CARD_NUMBER, "")).strip()
    card_holder = (await db.get_setting(KEY_CARD_HOLDER, "")).strip()
    cfg = SubscriptionConfig(enabled, price, days, card_number, card_holder)
    _config_cache = (now, cfg)
    return cfg


async def update_config(
    *,
    enabled: Optional[bool] = None,
    price: Optional[int] = None,
    days: Optional[int] = None,
    card_number: Optional[str] = None,
    card_holder: Optional[str] = None,
) -> SubscriptionConfig:
    # Avval hammasini VALIDATE qilamiz, keyin yozamiz — partial update yo'q.
    current = await get_config(fresh=True)
    next_enabled = current.enabled if enabled is None else bool(enabled)
    next_price = current.price if price is None else int(price)
    next_days = current.days if days is None else int(days)
    next_card = current.card_number if card_number is None else "".join(
        ch for ch in str(card_number) if ch.isdigit()
    )
    next_holder = current.card_holder if card_holder is None else " ".join(
        str(card_holder).strip().split()
    )[:100]

    if not 1_000 <= next_price <= 100_000_000:
        raise ValueError("price must be between 1000 and 100000000")
    if not 1 <= next_days <= 3650:
        raise ValueError("days must be between 1 and 3650")
    if next_card and not 12 <= len(next_card) <= 20:
        raise ValueError("card_number must contain 12-20 digits")
    if next_enabled and not next_card:
        raise ValueError("Premium rejimni yoqish uchun karta raqamini kiriting")
    if next_enabled and not next_holder:
        raise ValueError("Premium rejimni yoqish uchun karta egasini kiriting")

    if enabled is not None:
        await db.set_setting(KEY_ENABLED, "1" if next_enabled else "0")
    if price is not None:
        await db.set_setting(KEY_PRICE, str(next_price))
    if days is not None:
        await db.set_setting(KEY_DAYS, str(next_days))
    if card_number is not None:
        await db.set_setting(KEY_CARD_NUMBER, next_card)
    if card_holder is not None:
        await db.set_setting(KEY_CARD_HOLDER, next_holder)
    invalidate()
    return await get_config(fresh=True)


def _normalized_subscription(row: Optional[dict]) -> dict[str, Any]:
    data = dict(row or {})
    now = local_now()
    expires = None
    raw_exp = data.get("expires_at")
    if raw_exp:
        try:
            expires = datetime.fromisoformat(str(raw_exp))
        except ValueError:
            expires = None
    lifetime = bool(data.get("is_lifetime"))
    active = bool(data) and str(data.get("status") or "").upper() == "ACTIVE" and (
        lifetime or (expires is not None and expires > now)
    )
    remaining = 0
    if active and not lifetime and expires is not None:
        remaining = max(0, int((expires - now).total_seconds() // 86400) + 1)
    data.update({"active": active, "remaining_days": remaining, "is_lifetime": lifetime})
    return data


async def status(user_id: int, *, fresh: bool = False) -> dict[str, Any]:
    uid = int(user_id)
    now_mono = time.monotonic()
    cached = _status_cache.get(uid)
    if not fresh and cached and now_mono - cached[0] < CACHE_TTL:
        return dict(cached[2])
    data = _normalized_subscription(await db.get_subscription(uid))
    _status_cache[uid] = (now_mono, bool(data.get("active")), dict(data))
    return data


async def is_active(user_id: int) -> bool:
    return bool((await status(user_id)).get("active"))


async def grant_days(user_id: int, days: int, *, granted_by: int, note: str = "manual") -> dict[str, Any]:
    if not 1 <= int(days) <= 3650:
        raise ValueError("days must be between 1 and 3650")
    current = await status(user_id, fresh=True)
    if current.get("active") and current.get("is_lifetime"):
        return current
    now = local_now()
    current_exp = None
    if current.get("active") and not current.get("is_lifetime") and current.get("expires_at"):
        try:
            current_exp = datetime.fromisoformat(str(current["expires_at"]))
        except ValueError:
            current_exp = None
    base = current_exp if current_exp and current_exp > now else now
    starts = current.get("starts_at") or now.isoformat(timespec="seconds")
    expires = (base + timedelta(days=int(days))).isoformat(timespec="seconds")
    await db.save_subscription(
        int(user_id), status="ACTIVE", starts_at=str(starts), expires_at=expires,
        is_lifetime=False, granted_by=int(granted_by), note=note,
    )
    invalidate(int(user_id))
    return await status(int(user_id), fresh=True)


async def set_expiry(user_id: int, expires_at: str, *, granted_by: int) -> dict[str, Any]:
    try:
        expires = datetime.fromisoformat(str(expires_at))
    except ValueError as exc:
        raise ValueError("expires_at must be ISO datetime") from exc
    now = local_now()
    if expires <= now:
        raise ValueError("expires_at must be in the future")
    current = await status(user_id, fresh=True)
    await db.save_subscription(
        int(user_id), status="ACTIVE",
        starts_at=str(current.get("starts_at") or now.isoformat(timespec="seconds")),
        expires_at=expires.isoformat(timespec="seconds"), is_lifetime=False,
        granted_by=int(granted_by), note="manual-expiry",
    )
    invalidate(int(user_id))
    return await status(int(user_id), fresh=True)


async def grant_lifetime(user_id: int, *, granted_by: int) -> dict[str, Any]:
    now = local_now().isoformat(timespec="seconds")
    current = await status(user_id, fresh=True)
    await db.save_subscription(
        int(user_id), status="ACTIVE", starts_at=str(current.get("starts_at") or now),
        expires_at=None, is_lifetime=True, granted_by=int(granted_by), note="lifetime",
    )
    invalidate(int(user_id))
    return await status(int(user_id), fresh=True)


async def cancel(user_id: int, *, granted_by: int) -> dict[str, Any]:
    current = await status(user_id, fresh=True)
    await db.save_subscription(
        int(user_id), status="CANCELLED",
        starts_at=current.get("starts_at"), expires_at=current.get("expires_at"),
        is_lifetime=bool(current.get("is_lifetime")), granted_by=int(granted_by), note="cancelled",
    )
    invalidate(int(user_id))
    return await status(int(user_id), fresh=True)


async def create_payment(user_id: int, *, receipt_file_id: str, receipt_kind: str) -> dict[str, Any]:
    existing = await db.get_pending_payment_for_user(int(user_id))
    if existing:
        return dict(existing)
    cfg = await get_config()
    if not cfg.enabled:
        raise RuntimeError("subscription system is disabled")
    payment_id = uuid.uuid4().hex
    try:
        await db.create_payment_request(
            payment_id, int(user_id), cfg.days, cfg.price,
            str(receipt_file_id), str(receipt_kind),
        )
    except Exception:
        # Ikki chek deyarli bir vaqtda kelsa DB'dagi partial UNIQUE index
        # ikkinchi PENDING yozuvni to'xtatadi.  Mavjud arizani qaytaramiz.
        existing = await db.get_pending_payment_for_user(int(user_id))
        if existing:
            return dict(existing)
        raise
    row = await db.get_payment_request(payment_id)
    return dict(row or {"id": payment_id, "user_id": int(user_id), "status": "PENDING"})


async def pending_for_user(user_id: int) -> Optional[dict]:
    return await db.get_pending_payment_for_user(int(user_id))


async def approve_payment(payment_id: str, *, reviewed_by: int) -> dict[str, Any]:
    result = await db.approve_payment_request(str(payment_id), int(reviewed_by))
    if not result:
        raise ValueError("payment not found or already reviewed")
    invalidate(int(result["user_id"]))
    return dict(result)


async def reject_payment(payment_id: str, *, reviewed_by: int, reason: str = "") -> dict[str, Any]:
    result = await db.review_payment_request(
        str(payment_id), "REJECTED", int(reviewed_by), str(reason).strip()[:500]
    )
    if not result:
        raise ValueError("payment not found or already reviewed")
    return dict(result)


async def summary() -> dict[str, int]:
    return {
        "active": int(await db.count_active_subscriptions(local_now().isoformat(timespec="seconds"))),
        "pending": int(await db.count_payment_requests("PENDING")),
        "approved": int(await db.count_payment_requests("APPROVED")),
        "rejected": int(await db.count_payment_requests("REJECTED")),
    }
