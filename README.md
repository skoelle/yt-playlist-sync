# 🎬 yt-playlist-sync

Self-hosted service that watches the **public playlists of one YouTube channel** and downloads them as video with [yt-dlp](https://github.com/yt-dlp/yt-dlp) to a (NAS) share.

- ⏰ **Discovery every hour.** New playlists are queued and downloaded immediately.
- 🔄 **Sync playlists** (title does *not* contain the keyword) are synced once per night. A per-playlist yt-dlp download archive makes sure only new videos are fetched.
- 🎯 **Oneshot playlists** (title contains one of the keywords, default `setlist`, comma-separated, case-insensitive) are downloaded once. After a complete run they are never touched again. If something went wrong you get a status, an error summary and a retry button.
- 🚫 **Never deletes anything.** Playlists that disappear from YouTube are only marked `removed`.
- 🐌 One download at a time, no parallelism, with sleeps between videos to stay polite.
- 🖥️ Small web UI with three tabs: *Status*, *Sync-Playlists*, *Oneshot-Playlists*, plus a playlist detail page: cover image, a video gallery with thumbnails, duration badges and metadata from the local `.info.json` files (like a YouTube playlist), including resolution, file size, codecs and bitrates per video. Videos that are missing (private, deleted, failed or not fetched yet) appear as a placeholder row in their playlist position, with a broken-video symbol, a status badge and the last error message; the title links to YouTube. Clicking a video opens a built-in player that streams the downloaded file (seeking via HTTP range) and continues with the next video; click the background or press ESC to close. Also on the page: all database fields, job history, and the raw file list. Titles link to the detail page, a small ↗ opens the YouTube playlist. Data is reloaded with JavaScript, the page itself is never reloaded.
- 🔒 No Google login and no API keys: only public playlists are used.
- 🩺 Optional [Healthchecks](https://healthchecks.io) pings for the discovery run and the nightly sync.
- 📦 yt-dlp updates itself on start and daily, including the JS challenge solver; the extraction cache is cleared after an update. The image is rebuilt weekly by GitHub Actions.
- 🗄️ Nightly backup (00:30) of the database *and* the yt-dlp download archives, with rotation.

> ⚠️ **Disclaimer:** Only download content you own or that you are allowed to download. Respect the YouTube Terms of Service and copyright law. You are responsible for how you use this tool.

## 🚀 Quick start

```bash
mkdir -p /srv/docker/yt-playlist-sync/config
cd /srv/docker/yt-playlist-sync
curl -O https://raw.githubusercontent.com/skoelle/yt-playlist-sync/main/docker-compose.example.yml
mv docker-compose.example.yml docker-compose.yml
vim docker-compose.yml        # set the volume paths
vim .env                      # YOUTUBE_CHANNEL=@yourchannel, optional HC_* URLs
docker compose up -d
```

> 💡 **Tip:** Start with `DRY_RUN=1` first: discovery runs normally, but yt-dlp is called with `--simulate`, so nothing is written. When the list looks right, set `DRY_RUN=0`.

Open `http://<host>:8048`. The app has no login on purpose; put it behind a reverse proxy with authentication (for example Authelia) if you expose it.

The GHCR package is public: `docker pull ghcr.io/skoelle/yt-playlist-sync:latest` works without a login.

## 📂 Volumes

| Path | Purpose |
|---|---|
| `/config` | 🗄️ SQLite database, yt-dlp archives, job logs, self-updated yt-dlp. Use a **local** disk, not NFS (SQLite locking). |
| `/data` | 🎞️ Download target, for example your NAS share. The container user (`PUID`/`PGID`) needs write access. |
| `/backup` | 💾 Nightly backups: database and download archives (see below). Mount it, otherwise the copies stay inside the container. |

Download layout: `/data/<Playlist title> [<playlist id>]/<NN> - <Title> [<video id>].<ext>`. The folder name is fixed at the first download and does not change when a playlist is renamed. Next to each video you get thumbnail, `.info.json`, description and subtitles (where available).

## ⚙️ Configuration

| Variable | Default | Description |
|---|---|---|
| `YOUTUBE_CHANNEL` | required | Handle (`@name`), channel ID (`UC...`) or channel URL |
| `ONESHOT_KEYWORD` | `setlist` | Comma-separated keywords in the title that make a playlist a oneshot |
| `DISCOVERY_INTERVAL_MIN` / `DISCOVERY_INTERVAL_MAX` | `50` / `70` | Random minutes between discovery runs |
| `SYNC_CRON` | `0 3 * * *` | Nightly sync of sync playlists |
| `SYNC_JITTER` | `30` | Random minutes the nightly sync is delayed after `SYNC_CRON` (0 = off) |
| `YTDLP_UPDATE_CRON` | `30 2 * * *` | Daily yt-dlp update |
| `TZ` | `Europe/Berlin` | Time zone for schedules and UI |
| `DATA_DIR` / `CONFIG_DIR` | `/data` / `/config` | Paths inside the container |
| `DATABASE_URL` | SQLite in `/config` | Any SQLAlchemy URL, e.g. `mysql+pymysql://user:pass@host:3306/ytsync` |
| `PUID` / `PGID` | `1000` / `1000` | Owner of written files |
| `UMASK` | `0022` | File creation mask |
| `SLEEP_MIN` / `SLEEP_MAX` | `3` / `10` | Seconds to sleep between videos |
| `JOB_GAP_MIN` / `JOB_GAP_MAX` | `1` / `10` | Random seconds to wait between two playlists (jobs) |
| `YTDLP_EXTRA_ARGS` | empty | Extra yt-dlp arguments, for example `--limit-rate 5M` |
| `YTDLP_BIN` | `yt-dlp` | yt-dlp executable (tests use a stub) |
| `HC_DISCOVERY_URL` / `HC_SYNC_URL` | empty | Healthchecks ping URLs (`/start`, success, `/fail`) |
| `DRY_RUN` | `0` | `1` = list and simulate only |
| `LOG_LEVEL` | `INFO` | Log level |
| `LOG_RETENTION_DAYS` | `30` | Job log files older than this are removed (never videos) |
| `BACKUP_DIR` | `/backup` | Target directory for the nightly backups |
| `BACKUP_KEEP` | `7` | How many backups to keep per type, database copies and archive tarballs (`0` = unlimited) |

## 💾 Backup and restore

Every night at 00:30 (local `TZ`) the service writes two kinds of files into
`BACKUP_DIR`, both stamped `YYYYMMDD-HHMMSS`:

- **Database** — `app-YYYYMMDD-HHMMSS.db`, created with SQLite's backup API: the
  copy is consistent even while the app is writing (WAL) and needs no sidecar
  files. Each copy is checked with `PRAGMA integrity_check`.
- **Download archives** — `archives-YYYYMMDD-HHMMSS.tar.gz`, a tar.gz of
  `/config/archives/`. Tiny, but not regenerable: without them yt-dlp would
  re-check every video on the next run. Each tarball is read back fully as an
  integrity check.

The newest `BACKUP_KEEP` copies of **each type** are kept, older ones are
removed. Only SQLite is backed up — a MariaDB `DATABASE_URL` skips the database
part (the archives are always backed up).

🔁 To restore: stop the container, replace `/config/app.db` with the backup
file, delete stale `app.db-wal`/`app.db-shm` next to it, unpack the archive
tarball with `tar -xzf archives-<stamp>.tar.gz -C /config` (existing files are
only overwritten, nothing is deleted), start the container.

## 📝 Behaviour notes

- ✅ A oneshot counts as **done** when every video is in the yt-dlp archive or is permanently unavailable (private, deleted, age or members only). Otherwise it is **failed**; use *Retry* in the UI. Retries only fetch what is missing.
- 🔁 *Full Re-Run* re-checks a finished oneshot against the archive and downloads whatever is missing. Existing files and archives are never deleted.
- ⏸️ If YouTube answers with HTTP 429 or a bot check, the current job stops and the queue pauses for 30 minutes.
- 🚫 The same happens on HTTP 403 while downloading (`unable to download video data`): the job stops immediately instead of grinding through the playlist, the playlist shows *failed*, and a yt-dlp update check is triggered during the pause.
- 🔀 Playlists are never started back-to-back: the queue waits a random 1–10 seconds between two jobs (`JOB_GAP_MIN`/`JOB_GAP_MAX`), so yt-dlp process starts are not metronomic. The first job after an idle queue starts immediately.
- ⏭️ Discovery runs every 50–70 minutes on average (`DISCOVERY_INTERVAL_MIN`/`MAX`, each run picks a new random delay) and is skipped while any download is queued or running (including a rate-limit pause) — it retries at the next planned run. The manual *Run discovery* button always runs.
- 🌙 The nightly sync does not start exactly on the second: it begins a random 0–30 minutes after `SYNC_CRON` (`SYNC_JITTER`), a different offset every day.
- 🎞️ Format selection is `bv*+ba/b` (best available) merged to `mp4`, or `mkv` if the streams do not fit into mp4. Both work with Plex and Jellyfin.

## 🛠️ Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
DATA_DIR=./.dev/data CONFIG_DIR=./.dev/config DRY_RUN=1 YOUTUBE_CHANNEL=@yourchannel \
  uvicorn app.main:app --reload --port 8048
```

🧪 Tests use a stub (`tests/fixtures/fake_ytdlp.py`) instead of the real yt-dlp, so they need no network.

Dependency updates arrive as weekly Renovate PRs (Mondays before 6 am, minor/patch merged automatically).

See [SPEC.md](SPEC.md) for the specification, [PLAN.md](PLAN.md) for the implementation plan and [AGENTS.md](AGENTS.md) for the working rules.

## 📁 Project structure

```
app/
├── main.py, api.py          FastAPI app, REST endpoints, static UI serving
├── config.py, db.py         Settings (pydantic), engine, sessions, migrations
├── models.py, backup.py     Data model, nightly DB + download-archive backups with rotation
├── scheduler.py             Scheduled tasks: discovery interval (50–70 min), nightly sync (+ jitter), yt-dlp update, log cleanup, backup
├── jobqueue.py, runner.py   Single-worker queue and the yt-dlp subprocess runner
├── ytdlp.py, discovery.py   yt-dlp command builder/parsers, playlist discovery
├── healthchecks.py, paths.py  Ping helper, folder-name sanitising
└── static/                  index.html, app.js, style.css (vanilla, no build step)
migrations/                  Alembic schema migrations
tests/                       pytest suite; fixtures/fake_ytdlp.py is the network-free stub
.github/workflows/           test.yml (ruff + pytest), build.yml (weekly amd64 image)
renovate.json                weekly dependency update schedule
SPEC.md / PLAN.md / AGENTS.md  specification, implementation status, working rules
```

## 📜 License

Licensed under the [MIT License](LICENSE) - Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
