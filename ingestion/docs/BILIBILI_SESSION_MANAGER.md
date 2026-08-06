# Bilibili QR session manager

The Bilibili session manager uses Bilibili's official web QR endpoints. It
generates a short-lived QR image locally, sends it only to configured private
Telegram chats, polls for confirmation, and atomically exports a Netscape
cookie file with mode `0600`.

Set:

```dotenv
BILIBILI_COOKIES_FILE=/home/ubuntu/AI-Video-Transcriber/secrets/www.bilibili.com_cookies.txt
BILIBILI_SESSION_ENABLED=true
BILIBILI_SESSION_ROOT=/home/ubuntu/.local/share/bilibili-session
BILIBILI_LOGIN_TIMEOUT_SECONDS=180
BILIBILI_QR_TTL_SECONDS=180
BILIBILI_AUTO_RESUME=true
BILIBILI_LOGIN_CHAT_IDS=
```

`BILIBILI_LOGIN_CHAT_IDS` defaults to `TELEGRAM_ALLOWED_USER_IDS`. Bilibili
cookies are separate from Xiaohongshu cookies and are selected only for
Bilibili author discovery, metadata, subtitles, and media downloads.

When Bilibili returns HTTP 412, the ingestion worker probes the authenticated
navigation endpoint first. A QR is sent only when that probe confirms expired
authentication; a still-valid account remains classified as platform blocking
and does not trigger an unnecessary login. After QR confirmation, the manager
probes the published cookie file again and only then resumes Bilibili
single-video jobs, author discoveries, and auth-paused batches.

Install the user service:

```bash
install -m 0644 deploy/bilibili-session-manager.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

Start login from Telegram with `/video_login`, then choose “哔哩哔哩”, or run:

```bash
systemctl --user start bilibili-session-manager.service
```

The QR image is removed after success or timeout. Set
`BILIBILI_AUTO_RESUME=false` to disable automatic recovery while retaining QR
login. Cookie values and QR login keys are never printed, logged, or stored in
PostgreSQL.
