"""
Launcher: ``python run.py``

Kept as a separate one-liner file so deployment commands stay trivial
(systemd, Docker, `pm2 start run.py`, Railway start command, ...).

Startup xatosi bo'lsa NOL BO'LMAGAN exit code qaytariladi — platforma
(deploy/restart) nosog'lom konteynerni "muvaffaqiyatli" deb hisoblamaydi.
"""

from app.main import run_cli

if __name__ == "__main__":
    raise SystemExit(run_cli())
