"""
Centralised logging setup.

Call :func:`setup_logging` once at startup.  Every module then simply does
``logger = logging.getLogger(__name__)`` and inherits this configuration.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
LOG_FILE = LOG_DIR / "bot.log"
MAX_LOG_BYTES = 2 * 1024 * 1024  # 2 MB per file
LOG_BACKUPS = 3                  # bot.log.1 ... bot.log.3


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger; safe to call multiple times.

    Console (stdout) + rotating file ``logs/bot.log`` (2 MB x 3).
    Fayl yozib bo'lmasa (masalan, faqat-o'qish papka) — faqat konsol
    ishlaydi va bot ishga tushishda yiqilmaydi.
    """
    root = logging.getLogger()
    if root.handlers:  # already configured
        root.setLevel(level)
        return

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # Console stream: force UTF-8 so log lines containing emoji (bot names,
    # status icons) never raise UnicodeEncodeError on a cp1252 Windows
    # console.  ``errors="replace"`` keeps logging alive even on a stream
    # that cannot be reconfigured (e.g. a replaced stdout in tests).
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    try:
        LOG_DIR.mkdir(exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:  # noqa: BLE001 – fayl yozilmasa ham bot ishlaydi
        root.warning("Could not open log file %s - console logging only", LOG_FILE)

    root.setLevel(level)

    # aiogram is very chatty on DEBUG; keep INFO default but allow override.
    logging.getLogger("aiogram").setLevel(level)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
