# yt-playlist-sync

Self-hosted service that watches the **public playlists of one YouTube channel** and downloads them as video with [yt-dlp](https://github.com/yt-dlp/yt-dlp) to a (NAS) share.

- **Discovery every hour.** New playlists are queued and downloaded immediately.
- **Sync playlists** (title does *not* contain the keyword) are synced once per night. A per-playlist yt-dlp download archive makes sure only new videos are fetched.
- **Oneshot playlists** (title contains one of the keywords, default `setlist`, comma-separated, case-insensitive) are downloaded once. After a complete run they are never touched again. If something went wrong you get a status, an error summary and a retry button.
- **Never deletes anything.** Playlists that disappear from YouTube are only marked `removed`.
- One download at a time, no parallelism, with sleeps between videos to stay polite.
- Small web UI with three tabs: *Status*, *Sync-Playlists*, *Oneshot-Playlists*, plus a playlist detail page (all database fields, job history, files on disk). Titles link to the detail page, a small ↗ opens the YouTube playlist. Data is reloaded with JavaScript, the page itself is never reloaded.
- No Google login and no API keys: only public playlists are used.
- Optional [Healthchecks](https://healthchecks.io) pings for the discovery run and the nightly sync.
- yt-dlp updates itself on start and daily. The image is rebuilt weekly by GitHub Actions.

> **Disclaimer:** Only download content you own or that you are allowed to download. Respect the YouTube Terms of Service and copyright law. You are responsible for how you use this tool.

## Quick start

```bash
mkdir -p /srv/docker/yt-playlist-sync/config
cd /srv/docker/yt-playlist-sync
curl -O https://raw.githubusercontent.com/example-user/yt-playlist-sync/main/docker-compose.example.yml
mv docker-compose.example.yml docker-compose.yml
vim docker-compose.yml        # set the volume paths
vim .env                      # YOUTUBE_CHANNEL=@yourchannel, optional HC_* URLs
docker compose up -d
```

Start with `DRY_RUN=1` first: discovery runs normally, but yt-dlp is called with `--simulate`, so nothing is written. When the list looks right, set `DRY_RUN=0`.

Open `http://<host>:8048`. The app has no login on purpose; put it behind a reverse proxy with authentication (for example Authelia) if you expose it.

The GHCR package is private after the first push. Set it to *public* once in the GitHub package settings, otherwise `docker pull` needs a login.

## Volumes

| Path | Purpose |
|---|---|
| `/config` | SQLite database, yt-dlp archives, job logs, self-updated yt-dlp. Use a **local** disk, not NFS (SQLite locking). |
| `/data` | Download target, for example your NAS share. The container user (`PUID`/`PGID`) needs write access. |

Download layout: `/data/<Playlist title> [<playlist id>]/<NN> - <Title> [<video id>].<ext>`. The folder name is fixed at the first download and does not change when a playlist is renamed. Next to each video you get thumbnail, `.info.json`, description and subtitles (where available).

## Configuration

| Variable | Default | Description |
|---|---|---|
| `YOUTUBE_CHANNEL` | required | Handle (`@name`), channel ID (`UC...`) or channel URL |
| `ONESHOT_KEYWORD` | `setlist` | Comma-separated keywords in the title that make a playlist a oneshot |
| `DISCOVERY_CRON` | `0 * * * *` | Discovery schedule |
| `SYNC_CRON` | `0 3 * * *` | Nightly sync of sync playlists |
| `YTDLP_UPDATE_CRON` | `30 2 * * *` | Daily yt-dlp update |
| `TZ` | `Europe/Berlin` | Time zone for schedules and UI |
| `DATA_DIR` / `CONFIG_DIR` | `/data` / `/config` | Paths inside the container |
| `DATABASE_URL` | SQLite in `/config` | Any SQLAlchemy URL, e.g. `mysql+pymysql://user:pass@host:3306/ytsync` |
| `PUID` / `PGID` | `1000` / `1000` | Owner of written files |
| `UMASK` | `0022` | File creation mask |
| `SLEEP_MIN` / `SLEEP_MAX` | `3` / `10` | Seconds to sleep between videos |
| `YTDLP_EXTRA_ARGS` | empty | Extra yt-dlp arguments, for example `--limit-rate 5M` |
| `YTDLP_BIN` | `yt-dlp` | yt-dlp executable (tests use a stub) |
| `HC_DISCOVERY_URL` / `HC_SYNC_URL` | empty | Healthchecks ping URLs (`/start`, success, `/fail`) |
| `DRY_RUN` | `0` | `1` = list and simulate only |
| `LOG_LEVEL` | `INFO` | Log level |
| `LOG_RETENTION_DAYS` | `30` | Job log files older than this are removed (never videos) |

## Behaviour notes

- A oneshot counts as **done** when every video is in the yt-dlp archive or is permanently unavailable (private, deleted, age or members only). Otherwise it is **failed**; use *Retry* in the UI. Retries only fetch what is missing.
- *Full Re-Run* re-checks a finished oneshot against the archive and downloads whatever is missing. Existing files and archives are never deleted.
- If YouTube answers with HTTP 429 or a bot check, the current job stops and the queue pauses for 30 minutes.
- Format selection is `bv*+ba/b` (best available) merged to `mp4`, or `mkv` if the streams do not fit into mp4. Both work with Plex and Jellyfin.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
DATA_DIR=./.dev/data CONFIG_DIR=./.dev/config DRY_RUN=1 YOUTUBE_CHANNEL=@yourchannel \
  uvicorn app.main:app --reload --port 8048
```

Tests use a stub (`tests/fixtures/fake_ytdlp.py`) instead of the real yt-dlp, so they need no network.

See [SPEC.md](SPEC.md) for the specification and [PLAN.md](PLAN.md) for the implementation plan.

## License

Licensed under the [MIT License](LICENSE) - Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
