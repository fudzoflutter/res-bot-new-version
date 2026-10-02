"""
Havolalarni tozalovchi xizmat — HAMMA uchun, admin panellsiz.

Foydalanuvchi botga havola yuborsa, undan kuzatuv (tracking) parametrlari
olib tashlanadi: ``utm_*``, ``fbclid``, ``gclid``, ``yclid``, ``igshid``,
``ref`` va hokazo.  Natija — tozalangan (lekin hali ham uzun) havola.

MUHIM: bu "cleaner links only" varianti — qisqa domen YO'Q.  Haqiqiy
short-code xizmati alohida domen + hosting (masalan Railway) talab qiladi;
u keyinroq qo'shilishi mumkin.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Har doim olib tashlanadigan parametrlar (aynan shu nom bilan).
TRACKING_EXACT = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gclsrc",
        "yclid",
        "ysclid",
        "igshid",
        "igsh",
        "si",
        "ref",
        "ref_src",
        "ref_url",
        "mc_cid",
        "mc_eid",
        "_openstat",
        "mkt_tok",
        "vero_id",
        "wickedid",
        "_hsenc",
        "_hsmi",
        "sc_cid",
        "twclid",
        "ttclid",
        "li_fat_id",
        "s_kwcid",
        "msclkid",
        "spm",
        "scm",
    }
)

# Prefiks bo'yicha olib tashlanadiganlar (utm_source, utm_medium, ...).
TRACKING_PREFIXES = ("utm_",)

# Matndan havolalarni ajratib olish (http/https).
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered in TRACKING_EXACT or lowered.startswith(TRACKING_PREFIXES)


def clean_url(url: str) -> str:
    """Bitta havoladan kuzatuv parametrlarini olib tashlaydi.

    URL bo'lmasa (yoki o'zgarish bo'lmasa) — kirish qiymatini qaytaradi.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(k)
    ]
    new_query = urlencode(kept)
    # Bo'sh query bo'lsa '?' ham qolmasin.
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )


def find_urls(text: str | None) -> list[str]:
    """Matndagi barcha http(s) havolalarni topadi."""
    if not text:
        return []
    return URL_RE.findall(text)


def clean_text(text: str | None) -> list[tuple[str, str]]:
    """Matndagi o'zgargan havolalarni qaytaradi: [(asl, toza), ...].

    Faqat HAQIQATAN o'zgargan havolalar kiradi — shovqinsiz ishlash uchun.
    """
    changed: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url in find_urls(text):
        if url in seen:
            continue
        seen.add(url)
        cleaned = clean_url(url)
        if cleaned != url:
            changed.append((url, cleaned))
    return changed
