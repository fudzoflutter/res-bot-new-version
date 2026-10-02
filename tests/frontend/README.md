# Browser regression checks

From the repository root, serve static files only:

```sh
python3 -m http.server 8765 --bind 127.0.0.1
```

Open <http://127.0.0.1:8765/tests/frontend/>. The page runs automatically and displays PASS/FAIL results. It fetches the real `public/index.html` and `public/app.js`, then executes them in a fresh iframe per case with a fake Telegram object and intercepted `fetch`. No actual API, bot token, server application, or database is used. External scripts and styles are removed from the fixture. No dependency installation is required.

Coverage: Ban cancellation; broadcast cancellation, count, exact previewed payload, shared Send/Retry lock, empty audience; preservation of user DOM nodes/focus; stale search responses; out-of-order broadcast status; initial user-load errors.
