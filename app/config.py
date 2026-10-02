"""
Central configuration for the whole bot.

Every tunable value lives here (or in ``.env``) so that you never have to dig
through handler files to change something.  ``settings`` is created once at
import time and validated immediately: if something is missing the bot tells
you exactly what instead of failing later in a random handler.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values, load_dotenv

# Load secret keys BEFORE reading the values below.
BASE_DIR = Path(__file__).resolve().parent.parent
# Ikki manba, BIR XIL format (KEY=VALUE):
#   env.txt  – foydalanuvchi kalitlari (BOT_TOKEN, ADMIN_ID, SUPABASE_DB_URL...)
#   .env     – zaxira/klassik fayl
# dotenv mavjud kalitlarni BOSIB YOZMAYDI, shuning uchun BIRINCHI o'qilgan
# fayl g'alaba qiladi.  env.txt birinchi turadi — yaqinda to'ldirilgan
# qiymatlar eskirgan .env qiymatlarini teskari bosib olmaydi.
#
# encoding="utf-8-sig" — Windows Notepad bilan saqlangan fayldagi YASHIRIN
# BOM belgisini yutadi.  Bomsiz fayl bo'lsa ham zarari yo'q; BOM bilan
# esa aks holda BIRINCHI satrdagi kalit (masalan BOT_TOKEN) o'qilmaydi!
# TEST rejimi: tests/ fayllari buni '1' qilib o'rnatadi — haqiqiy env.txt/.env
# O'QILMAYDI (aks holda testlar PRODUCTION Supabasega yozib yuboradi).
_SKIP_ENV_FILES = os.getenv("CODEBUFF_SKIP_ENV_FILE", "").strip() == "1"

if not _SKIP_ENV_FILES:
    load_dotenv(BASE_DIR / "env.txt", encoding="utf-8-sig")
    load_dotenv(BASE_DIR / ".env", encoding="utf-8-sig")


def _load_utf16_fallback(path: Path) -> None:
    """Windows Notepad 'Unicode' rejimi faylni UTF-16 qilib saqlaydi.

    Bunday faylni utf-8-sig ham o'qiy olmaydi (kalitlar 0 ta bo'ladi).
    Fayl boshi FF FE / FE FF bilan boshlansa — UTF-16 deb qayta o'qiydi.
    """
    if _SKIP_ENV_FILES:
        return
    try:
        raw = path.read_bytes()
    except OSError:
        return
    if raw[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        return
    try:
        text = raw.decode("utf-16")
        load_dotenv(io.StringIO(text), override=False)
    except (UnicodeDecodeError, ValueError):
        pass


_load_utf16_fallback(BASE_DIR / "env.txt")
_load_utf16_fallback(BASE_DIR / ".env")
# Ixtiyoriy: ENV_FILE=/yol/kalitlar.txt — platforma (Docker/hosting)
# maxfiy kalitlarni boshqa joyga qo'ysa, shu o'zgaruvchi orqali ko'rsatiladi.
# Ustuvorligi ENG BALAND (override=True) — ataylab ko'rsatilgan fayl g'alaba qiladi.
_env_file_override = os.getenv("ENV_FILE", "").strip()
if _env_file_override and not _SKIP_ENV_FILES:
    load_dotenv(_env_file_override, encoding="utf-8-sig", override=True)


def _env(key: str, default: str = "") -> str:
    """Small helper so the dataclass below stays readable."""
    import os

    return os.getenv(key, default).strip()


def _env_int(key: str, default: int) -> int:
    """Read an integer from the environment, falling back to `default`."""
    import os

    raw = os.getenv(key, "").strip()
    return int(raw) if raw.isdigit() else default


def _env_ids(key: str) -> tuple[int, ...]:
    """Read a comma/space separated list of Telegram user IDs from the env."""
    raw = os.getenv(key, "")
    ids: list[int] = []
    for chunk in raw.replace(",", " ").split():
        value = chunk.strip()
        if value.lstrip("-").isdigit():
            number = int(value)
            if number != 0 and number not in ids:
                ids.append(number)
    return tuple(ids)


def _environment() -> str:
    """Joriy muhit nomi: ``production`` | ``staging`` | ``development``.

    STAGING va PRODUCTION bir-biridan FAQAT muhit o'zgaruvchilari bilan
    ajratiladi (bot token, baza, WebApp URL, adminlar) — kodda hech qanday
    muhitga xos qiymat YO'Q.  Railway'da har bir environment o'z
    o'zgaruvchilar to'plamini beradi.
    """
    raw = os.getenv("ENVIRONMENT", os.getenv("APP_ENV", "")).strip().lower()
    if raw in ("prod", "production", "live"):
        return "production"
    if raw in ("stage", "staging", "preprod", "pre-production"):
        return "staging"
    if raw in ("dev", "development", "local", "test", "testing"):
        return raw
    # ENVIRONMENT ko'rsatilmagan: Railway production muhitini aniqlaymiz.
    if os.getenv("RAILWAY_ENVIRONMENT", "").strip():
        return "production"
    return "development"


def _is_production() -> bool:
    """Best-effort production detection (Railway / Render / explicit flag).

    Bu qiymat faqat OGOHLANTIRISH uchun: masalan, productionda
    ``FORCE_POLL=1`` yoqilib qolgan bo'lsa admin panelda qizil ogohlantirish
    ko'rinadi.
    """
    # Aniq ko'rsatilgan muhit ENG USTUVOR: staging hech qachon production
    # deb hisoblanmaydi (ogohlantirishlar chalkashmasin).
    if _environment() in ("staging", "development", "local", "test", "testing"):
        return False
    for key in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RENDER_SERVICE_ID"):
        if os.getenv(key, "").strip():
            return True
    env = os.getenv("ENVIRONMENT", os.getenv("APP_ENV", "")).strip().lower()
    if env in ("production", "prod", "live"):
        return True
    debug = os.getenv("DEBUG", "").strip().lower()
    if debug in ("0", "false", "no"):
        return True
    return False


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of all configuration values."""

    # --- Telegram -----------------------------------------------------------
    bot_token: str = field(default_factory=lambda: _env("BOT_TOKEN"))
    admin_id: int = field(default_factory=lambda: _env_int("ADMIN_ID", 0))
    
    # --- Adminlar (ko'p adminli arxitektura) ---------------------------------
    # ADMIN_ID — OWNER (barcha sozlamalar, destruktiv amallar, admin
    # boshqaruvi).  ADMIN_IDS — qo'shimcha ADMINlar (monitoring, statistikа,
    # jurnallar, ulanish holati — o'qish/boshqarish).
    # Har bir amal handler ichida SERVER-SIDE tekshiriladi.
    admin_ids_extra: tuple[int, ...] = field(default_factory=lambda: _env_ids("ADMIN_IDS"))
    owner_ids_extra: tuple[int, ...] = field(default_factory=lambda: _env_ids("ADMIN_OWNER_IDS"))

    # --- Muhit (ogohlantirishlar + staging/production ajratish) -------------
    environment: str = field(default_factory=_environment)
    production: bool = field(default_factory=_is_production)

    # --- Bir nusxa qulfi (takroriy 409 ni to'xtatadi) ------------------------
    # Bot ishga tushganda bazadagi instance_lock ga "men tirikman" deb yozadi.
    # Boshqa TIRIK nusxa bo'lsa, yangi nusxa pollingni boshlamaydi (sababini
    # aytib to'xtaydi) — Telegram baribir faqat bittasiga update beradi.
    # FORCE_POLL=1 — qulfni o'chirib yuboradi (ikki nusxa birga ishlasa
    # xabarlar bo'linib ketishini HISOBGA OLGAN holda).
    force_poll: bool = field(default_factory=lambda: _env("FORCE_POLL", "0") == "1")

    # --- Storage ------------------------------------------------------------
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "bot.db"))

    # --- Supabase Postgres (primary storage) --------------------------------
    # .env dagi SUPABASE_DB_URL - Supabase Dashboard -> Connect ->
    # "Connection pooling" URI (postgresql://postgres.xxxx:...:6543/postgres).
    supabase_db_url: str = field(default_factory=lambda: _env("SUPABASE_DB_URL"))

    # --- Activity report layout -----------------------------------------------
    # Sarlavha bilan asosiy qatorlar ORASIDAGI bo'sh qatorlar soni.
    # 1 = "✏️ Xabar tahrirlandi" va "👤 Kim:" orasida BITTA bo'sh qator
    # (hozirgi ko'rinish).  0 = yopiq, 2 = juda keng.
    report_line_gap: int = 1

    # EMOJILAR endi app/emoji_config.py da (yagona ro'yxat) — bu yerda emas.

    @property
    def supabase_host(self) -> str:
        """Short host name for logs (no password inside)."""
        try:
            from urllib.parse import urlparse

            return urlparse(self.supabase_db_url).hostname or "?"
        except Exception:  # noqa: BLE001
            return "?"

    @property
    def is_configured(self) -> bool:
        """True when required values are present."""
        return bool(self.bot_token) and self.admin_id > 0

    @property
    def owner_ids(self) -> frozenset[int]:
        """Owner (ega) IDlari — ADMIN_ID + ADMIN_OWNER_IDS."""
        ids = set(self.owner_ids_extra)
        if self.admin_id > 0:
            ids.add(self.admin_id)
        return frozenset(ids)

    @property
    def admin_ids(self) -> frozenset[int]:
        """Barcha admin IDlari (owner + admin)."""
        return self.owner_ids | frozenset(self.admin_ids_extra)

    def role_of(self, user_id: int) -> Optional[str]:
        """``"owner"`` | ``"admin"`` | ``None`` — server-side rol tekshiruvi."""
        if user_id in self.owner_ids:
            return "owner"
        if user_id in self.admin_ids:
            return "admin"
        return None

    def missing_keys(self) -> list[str]:
        """List of human readable names of required-but-missing settings."""
        missing: list[str] = []
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.admin_id:
            missing.append("ADMIN_ID")
        return missing



settings = Settings()


def ensure_configured() -> None:
    """Raise a clear, self-diagnosing error when required keys are missing."""
    if settings.is_configured:
        return
    missing = ", ".join(settings.missing_keys())

    # Tashxis: qaysi kalit-fayllar qaraldi, topildimi, nechta qator o'qildi?
    lines = [f"Missing required settings: {missing}.", "", "Bot kalitlarni shu joylardan qidiradi:"]
    for name in ("env.txt", ".env"):
        path = BASE_DIR / name
        if path.exists():
            try:
                n = len(dotenv_values(path, encoding="utf-8-sig"))
            except (UnicodeDecodeError, ValueError):
                n = 0
            if n:
                lines.append(f"  • {name}: TOPILDI, {n} ta kalit o'qildi — lekin BOT_TOKEN/ADMIN_ID topilmadi (ism yoki format xato)")
            else:
                lines.append(f"  • {name}: TOPILDI, lekin 0 ta kalit o'qildi (format yoki kodirovka xato — UTF-16?)")
        else:
            lines.append(f"  • {name}: YO'Q (bu fayl topilmadi)")
    lines.append("  • ENV_FILE muhit o'zgaruvchisi (ko'rsatilmagan)" if not _env_file_override else f"  • ENV_FILE={_env_file_override}")
    lines.append("  • To'g'ridan-to'g'ri muhit o'zgaruvchalari (BOT_TOKEN, ADMIN_ID)")
    lines += [
        "",
        "Fayl formati — har satrda BITTA kalit, '=' belgisi bilan:",
        "  BOT_TOKEN=123456:ABC-DEF...",
        "  ADMIN_ID=123456789",
        "  SUPABASE_DB_URL=postgresql://...",
        "",
        "ETIHBOR: agar fayl TOPILDI lekin 0 ta kalit o'qilgan bo'lsa —",
        "format xato (masalan 'ADMIN_ID: 123' yoki 'ADMIN ID=123').",
        "env.txt fayli kod bilan BIRGA deploy qilingan bo'lishi shart",
        "(konteyner ichida /app/env.txt bo'lishi kerak).",
    ]
    raise RuntimeError("\n".join(lines))
