"""
Database health tracking (admin panel + alerts uchun).

Bu modul MA'LUMOT yig'adi, boshqaruvni o'zi qilmaydi:

* har bir DB so'rovi natijasi (muvaffaqiyat / xato) shu yerga yoziladi;
* ``app/services/watchdog.py`` va ``app/services/alerts.py`` shu yerdan holatni o'qiydi;
* ``app/services/alerts.py`` xatolar bo'yicha adminga xabar yuboradi.

MAXFIYLIK: xato matni saqlashdan OLDIN tozalanadi — DSN/parol hech qachon
jurnalga ham, admin panelga ham tushmaydi (:func:`sanitize`).

DIQQAT: bu yerda hech qanday I/O yo'q (faqat xotira) — shuning uchun bu
modulni har qanday qatlamdan xavfsiz chaqirish mumkin, hatto DB o'lgan
paytda ham.
"""

from __future__ import annotations

from app.utils.timeutils import local_now

import re
from datetime import datetime
from typing import Optional

# DSN: postgresql://user:parol@host:port/db -> parolni yashiramiz.
_DSN_PASSWORD = re.compile(r"(://[^/@\s:]+):[^@/\s]*@")
# Supabase/API kalitlari: "sb_secret_...", "eyJ..." (JWT), "sk-..."
_SECRETS = re.compile(
    r"(sb_[a-z]+_[A-Za-z0-9_\-]{6,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-\.]+|"
    r"(?:api[_-]?key|token|password|secret)\s*[=:]\s*\S+)",
    re.IGNORECASE,
)
MAX_ERROR_CHARS = 200

# Backend nomlari (admin panelda ko'rsatiladi).
BACKEND_POSTGRES = "Supabase Postgres"
BACKEND_SQLITE = "SQLite (fallback)"
BACKEND_NONE = "—"


def sanitize(text: str) -> str:
    """Xato matnidan parol/kalitlarni olib tashlaydi (jurnalga xavfsiz)."""
    cleaned = _DSN_PASSWORD.sub(r"\1:***@", str(text or ""))
    cleaned = _SECRETS.sub("***", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:MAX_ERROR_CHARS]


class DbHealth:
    """DB qatlamining joriy holati (protsess ichida)."""

    def __init__(self) -> None:
        self.backend: str = BACKEND_NONE
        self.connected: bool = False
        self.fallback_active: bool = False
        self.last_ok_at: Optional[datetime] = None
        self.last_error_at: Optional[datetime] = None
        self.last_error: str = ""
        self.last_op: str = ""
        self.consecutive_failures: int = 0
        self.total_errors: int = 0
        self.total_queries: int = 0
        self.total_retries: int = 0
        self.reconnects: int = 0

    # -- yozuv --------------------------------------------------------------
    def set_backend(self, name: str, *, fallback: bool = False) -> None:
        self.backend = name
        self.fallback_active = fallback

    def record_success(self, op: str = "") -> None:
        self.connected = True
        self.last_ok_at = local_now()
        self.last_op = op
        self.consecutive_failures = 0
        self.total_queries += 1

    def record_error(self, exc: BaseException, *, op: str = "") -> None:
        """Xatoni qayd etadi (hali 'o'lik' deb belgilamaydi)."""
        self.total_errors += 1
        self.last_error = sanitize(f"{type(exc).__name__}: {exc}")
        self.last_error_at = local_now()
        self.last_op = op

    def record_failure(self, exc: BaseException, *, op: str = "") -> None:
        """Qayta urinishlar tugagach ham xato — ulanish 'uzilgan' hisoblanadi."""
        self.record_error(exc, op=op)
        self.consecutive_failures += 1
        self.connected = False

    def record_retry(self) -> None:
        self.total_retries += 1

    def record_reconnect(self) -> None:
        self.reconnects += 1

    def mark_down(self, reason: str = "") -> None:
        self.connected = False
        if reason:
            self.last_error = sanitize(reason)
            self.last_error_at = local_now()

    # -- o'qish -------------------------------------------------------------
    @property
    def status_icon(self) -> str:
        if self.connected:
            return "🟢"
        return "🔴"

    def snapshot(self) -> dict:
        """Admin panel uchun qisqa holat."""
        return {
            "backend": self.backend,
            "connected": self.connected,
            "status_icon": self.status_icon,
            "fallback": self.fallback_active,
            "last_ok_at": self.last_ok_at.strftime("%d.%m.%Y %H:%M:%S") if self.last_ok_at else "—",
            "last_error_at": self.last_error_at.strftime("%d.%m.%Y %H:%M:%S") if self.last_error_at else "—",
            "last_error": self.last_error or "—",
            "last_op": self.last_op or "—",
            "consecutive_failures": self.consecutive_failures,
            "total_errors": self.total_errors,
            "total_queries": self.total_queries,
            "total_retries": self.total_retries,
            "reconnects": self.reconnects,
        }


# Global holat (butun protsess uchun bitta).
health = DbHealth()
