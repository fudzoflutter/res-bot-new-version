"""
Telegram update handlers, split by audience:

* :mod:`app.handlers.user`        – /start, menu, stats, connect
* :mod:`app.handlers.admin`       – /admin: Web Mini App (TMA) tugmasi (FAQAT admin)
* :mod:`app.handlers.business`    – business connection + activity reports

Kirish nazorati (allow/deny/ban) OLIB TASHLANGAN.
Admin panel — Web Mini App; har bir so'rov serverda initData + rol bilan tekshiriladi (app/web.py).
"""
