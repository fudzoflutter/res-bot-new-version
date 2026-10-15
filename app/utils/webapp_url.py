"""Resolve admin Mini App URL without a deployment-specific default."""
from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit


def webapp_url() -> str:
    """WEBAPP_URL overrides the current Railway public domain."""
    raw = (os.getenv("WEBAPP_URL") or os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if not raw:
        raise RuntimeError(
            "Admin WebApp URL topilmadi. Railway Settings > Networking > "
            "Public Networking bo'limida Generate Domain qiling yoki "
            "WEBAPP_URL=https://your-domain.up.railway.app/ ni o'rnating."
        )
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlsplit(raw)
        _ = parsed.port  # Reject invalid ports.
    except ValueError as exc:
        raise ValueError(f"Admin WebApp URL noto'g'ri: {raw!r}") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(char.isspace() for char in raw)
    ):
        raise ValueError(
            "Admin WebApp manzili HTTPS URL bo'lishi shart "
            f"(masalan, https://example.up.railway.app/). Hozir: {raw!r}"
        )
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, parsed.fragment))
