# Xiaohongshu session manager

The session manager keeps Xiaohongshu authentication in an isolated persistent
Chromium profile. The browser is used only for authentication; author discovery,
signing, media download, and transcription continue to use the existing
`urllib` and `xhshow` path.

## Installation

Install Python dependencies and the matching browser:

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
sudo apt-get install xvfb
```

Set `XIAOHONGSHU_COOKIES_FILE` to the existing local cookie file, set the
private `XIAOHONGSHU_LOGIN_CHAT_IDS`, and enable
`XIAOHONGSHU_SESSION_ENABLED=true`. Cookie, profile, status, lock, and QR files
are created with private permissions. Do not place the session root inside the
repository.

Install the user units:

```bash
install -m 0644 deploy/xhs-session-manager.service ~/.config/systemd/user/
install -m 0644 deploy/xhs-session-check.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now xhs-session-check.timer
```

## Commands

```bash
xvfb-run -a .venv/bin/python -m backend.xhs_session.cli status --probe
xvfb-run -a .venv/bin/python -m backend.xhs_session.cli refresh
xvfb-run -a .venv/bin/python -m backend.xhs_session.cli login
xvfb-run -a .venv/bin/python -m backend.xhs_session.cli export-cookies
.venv/bin/python -m backend.xhs_session.cli cleanup
```

`status` without `--probe` is read-only and reports the last safe state. A
present cookie file is never reported as a valid session unless the real probe
has succeeded. `refresh` exports cookies only after a successful probe and
probes again after the atomic replacement. If authentication has expired, it
sends a short-lived QR image to the configured Telegram chats and waits for a
phone scan.

An `AUTH_EXPIRED` access failure safely defers the affected Xiaohongshu work and
asks systemd to start the singleton manager. This applies to author discovery,
individual video ingestion, and batch ingestion. If the persistent browser
session is also expired, the manager sends the login QR automatically. After a
successful scan and verified probe, it wakes the deferred discovery, individual
job, or current batch item immediately without consuming a normal processing
retry. Set `XIAOHONGSHU_AUTO_RESUME=false` to require operator-controlled
recovery.

Rate limiting, bot checks, platform blocking, and network failures remain
distinct and do not generate a login QR.

The Telegram `/video_login` command can also start this flow explicitly.
An `UNKNOWN` browser probe is treated as requiring interactive recovery unless
a rate limit, bot check, platform block, network failure, or corrupt profile
was positively detected. This prevents stale cookies from remaining active
merely because a changed page shape made the lightweight probe inconclusive.

Never expose VNC, noVNC, or Chromium remote-debugging ports. If emergency visual
access is needed, bind it only to `127.0.0.1` and use an SSH tunnel.
