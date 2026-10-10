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
- 📦 yt-dlp updates itself on start and daily, including the JS challenge solver; the extraction cache is cleared after an update. The update is atomic (staged install, symlink swap) and queued jobs wait while the library is replaced. The image is rebuilt weekly by GitHub Actions.
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
- ❓ Discovery never marks a playlist as *removed*: a playlist that is missing from the channel listing (unlisted, manually added, imported) keeps its type and keeps syncing. Only a sync run can confirm that a playlist is gone – then it becomes `removed` and switches from *sync* to *oneshot* (HTTP 403/429, network or age errors never do that). When it reappears, only the status returns to *active*, the type stays *oneshot*.

## ➕ Adding a playlist manually

Playlists that never show up in the channel listing – unlisted, removed from the channel, or belonging to someone else – can be added by hand:

1. Open the **Sync** or **Oneshot** tab; the type follows the tab.
2. Paste the playlist URL (`…/playlist?list=…`, any watch URL with `&list=…`, or the bare playlist ID), optionally enter a title, click **Add playlist**.
3. The app validates the playlist with yt-dlp (no login, works for unlisted) and adds a row marked `manual`. Nothing is downloaded yet – use **Sync now** / **Download now** like for any other playlist.

Manually added sync playlists run in the nightly sync like every other sync playlist; oneshots wait for the button. Errors (private, deleted, already added) appear next to the form. Existing rows are never touched or deleted.

## 🗄️ Exporting from TubeArchivist

An existing TubeArchivist library can be exported into the same folder layout this project
downloads into (`<Playlist title> [<playlist id>]/NN - <Title> [<video id>].mp4`), so it can be
archived, inspected or imported later without touching the app, its database or YouTube.

```bash
# values live in .env (gitignored): TA_API_URL, TA_API_TOKEN, TA_API_HOST,
#                                   TA_EXPORT_TARGET, TA_MEDIA_ROOT
.venv/bin/python -m app.ta_export --dry-run --metadata-only     # report only
.venv/bin/python -m app.ta_export --metadata-only               # metadata, no videos
.venv/bin/python -m app.ta_export                               # full copy
.venv/bin/python -m app.ta_export --playlist PLxxxx --mode hardlink
```

| Option | Purpose |
|---|---|
| `--target` | export root (the folder the playlist directories are written to) |
| `--media-root` | TubeArchivist data directory (contains `media/` and `cache/`) |
| `--metadata-only` | write metadata, cover, archive and `manifest.json`, but no video files |
| `--dry-run` | write nothing at all, only print the report |
| `--mode auto\|copy\|hardlink` | default `auto`: try a hardlink and fall back to a copy when the filesystem refuses it (Synology NFS answers `Operation not permitted`); `hardlink` makes that a hard error |
| `--playlist ID` | export a single playlist (repeatable) |
| `--host-header` | `Host` header override; needed when TubeArchivist rejects the URL host |
| `--no-archives` | skip the `archives/<playlist id>.txt` files |

Each playlist folder gets a `manifest.json` (playlist id, title, item count, per video position,
id, title, duration, publication date and file name) plus yt-dlp style `.info.json` sidecars, a
`00 - <title> [<playlist id>].jpg` cover and `<target>/archives/<playlist id>.txt` with the
`youtube <video id>` lines a later sync or import would need. The export never deletes or
overwrites anything: reruns skip files that already exist, and entries whose media file is
missing from the library are reported instead of archived. Files are written to a temporary
`<name>.part` and renamed afterwards, so an interrupted run never leaves a truncated video
under its final name; a rerun also compares sizes and rewrites files that are incomplete
(counted as `rep`/`repaired` in the report).

### Running the export inside the container

The image ships `app/ta_export.py`, so a full export can run where the library lives instead
of streaming it over the network twice. Mount the TubeArchivist folder read-only and the
export folder read-write. `--mode auto` first tries to hardlink (report column `link`), but
inside a container the two bind mounts often behave like separate filesystems, so it falls
back to copying (report column `copy`) – that works, it just uses the space twice. The
entrypoint runs the command as `PUID`/`PGID`, so the exported files are never owned by root.

```bash
docker run --rm \
  -e PUID=1000 -e PGID=1000 \
  -e TA_API_URL=http://<tubearchivist-host>:8770 \
  -e TA_API_HOST=<allowed-host-header> \
  -e TA_API_TOKEN=<token> \
  -e TA_MEDIA_ROOT=/ta \
  -e TA_EXPORT_TARGET=/export \
  -v /path/to/tubearchivist:/ta:ro \
  -v /path/to/export:/export \
  ghcr.io/skoelle/yt-playlist-sync:latest \
  python -m app.ta_export --dry-run
```

Run it with `--dry-run` first (report only), then `--metadata-only` and finally without
flags for the videos. There is no `.env` inside the image, so every value is passed with
`-e` (or point `--env-file` at a mounted file).

## 📺 Exporting from TubeSync

[TubeSync](https://github.com/TubeSync/tubesync) has no JSON API, so `app.ts_export.py` opens
its SQLite database **read-only** (`sync_source`, `sync_media`, `sync_media_metadata`) and pairs
the rows with the files below its `downloads/` folder. The result is the same folder layout as
above, ready for `app.ta_import.py`:

```bash
# values live in .env (gitignored): TS_DB, TS_MEDIA_ROOT, TS_THUMBS_ROOT, TS_EXPORT_TARGET
.venv/bin/python -m app.ts_export --dry-run --metadata-only   # report only
.venv/bin/python -m app.ts_export --metadata-only             # metadata, no videos
.venv/bin/python -m app.ts_export                             # full copy
.venv/bin/python -m app.ts_export --playlist PLxxxx --mode copy
```

| Option | Purpose |
|---|---|
| `--db` | path of TubeSync's `db.sqlite3` (always opened read-only) |
| `--media-root` | TubeSync `downloads/` directory (contains `video/`) |
| `--thumbs-root` | TubeSync `media/` directory holding `thumbs/`; defaults to the directory next to the database (`config/media`) |
| `--target` | export root (the folder the playlist directories are written to) |
| `--metadata-only`, `--dry-run`, `--mode`, `--playlist ID`, `--no-archives` | same meaning as in the TubeArchivist export |

Details worth knowing:

- **Order:** TubeSync stores no playlist position (the `playlist_index` field in its metadata JSON
  is empty), so videos are numbered in the order TubeSync crawled them (`created`, then
  `published`). A metadata row that does carry a `playlist_index` wins over that.
- **Titles and metadata** come from `sync_media`/`sync_media_metadata`. The description stays in
  the TubeSync database and is not copied into every `.info.json` – the gallery never reads it.
- **Thumbnails** are copied from `thumbs/<xx>/<uuid>.jpg` to `NN - <title> [<video id>].jpg` next
  to the video. TubeSync keeps no playlist cover, so the gallery shows no cover there.
- Rows without a media file (TubeSync only writes `media_file` once a download exists, and a
  file can vanish) are exported as placeholders (`file: null`, `downloaded: false`) and counted
  as `miss` – exactly what the importer expects.
- Sources that are not playlists (`source_type != 'p'`) are skipped with a warning.
- Everything else – the report, the archives, `manifest.json`, hardlink/copy behaviour and the
  promise to never delete or overwrite anything – is identical to the TubeArchivist export.

The same container example works for TubeSync, only the environment and the mounts differ:

```bash
docker run --rm \
  -e PUID=1000 -e PGID=1000 \
  -e TS_DB=/ta/config/db.sqlite3 \
  -e TS_MEDIA_ROOT=/ta/downloads \
  -e TS_THUMBS_ROOT=/ta/config/media \
  -e TS_EXPORT_TARGET=/export \
  -v /path/to/tubesync:/ta:ro \
  -v /path/to/export:/export \
  ghcr.io/skoelle/yt-playlist-sync:latest \
  python -m app.ts_export --dry-run
```

The database is opened read-only, so TubeSync can keep running; if SQLite still refuses to read
because of a pending transaction, stop TubeSync and run the export again.

## 📥 Importing the exported tree

The export is only a folder tree until the app knows about it. `app.ta_import.py` reads the
`manifest.json` files and writes playlists, video entries and download archives into the app
database – offline, without YouTube and without the TubeArchivist API. **Stop the app first**
(the importer needs exclusive database access), then move the playlist folders into the data
directory and run:

```bash
# 1. preview while the folders are still in the export root
docker run --rm -e PUID=1000 -e PGID=1000 \
  -v /path/to/export:/export:ro -v /path/to/data:/data -v /path/to/config:/config \
  ghcr.io/skoelle/yt-playlist-sync:latest \
  python -m app.ta_import --source /export --dry-run

# 2. folders are in /data now: plain run (default source = data dir)
... python -m app.ta_import
```

| Option | Purpose |
|---|---|
| `--source` | root that is scanned for `<folder>/manifest.json` (default: data directory) |
| `--playlist ID` | import only this playlist (repeatable; wins over `--skip`) |
| `--skip ID` | leave out this playlist (repeatable) |
| `--no-archives` | do not append local files to `config/archives/<id>.txt` |
| `--dry-run` | report only; no playlist, entry or archive is written |

Rules the importer follows:

- Every imported playlist becomes **oneshot** (frozen – the nightly sync never picks it up;
  switch the type back in the UI if you want it synced).
- **Local files decide**: `done` only when every manifest entry has a file on disk, otherwise
  `idle`, so missing videos can be downloaded later with *Download now*/*Full Re-Run*.
- An **update never downgrades**: a playlist you already brought to `done` (or `failed`) keeps
  that state even if the folder is incomplete; only a complete folder sets `done`.
- Entries are upserted, never removed. `reason`/`unavailable` from earlier YouTube runs are
  never overwritten; rows that are not in the manifest stay untouched.
- Every local video file gets an append-only `youtube <id>` line in
  `config/archives/<id>.txt` – no need to copy the export's `archives/` folder, and reruns
  are idempotent (`manifest.json` stays in the playlist folder as the marker).
- Nothing is ever moved, renamed or deleted; the importer only writes database rows and
  appends archive lines.

If a playlist no longer exists on YouTube, it shows up as *removed* + *oneshot* only after you
run it once manually (see the sync rule above); until then it simply sits in the oneshot tab.

## 🧹 Forgetting a playlist (maintenance)

`forget` removes every trace of one or more playlists while the application is stopped: the
database row with its listing entries and jobs, the download archives in `/config/archives`
and `/data/archives`, and the playlist folder – which is moved into `/data/.quarantine`
instead of being deleted. Use it before re-importing the same playlist from another source:
the importer alone would keep the stale listing rows, the old download archive and the old
files next to the new ones.

```bash
docker compose stop yt-playlist-sync

# 1. preview (the default; nothing is written)
docker compose run --rm yt-playlist-sync python -m app.forget --playlist PLxxxxxxxx

# 2. apply: database/archives backup first, then folder -> archives -> database rows
docker compose run --rm yt-playlist-sync python -m app.forget --playlist PLxxxxxxxx --apply

docker compose up -d
```

| Option | Purpose |
|---|---|
| `--playlist ID` | playlist to forget (repeatable, required) |
| `--quarantine DIR` | where folders are moved (default: `/data/.quarantine`) |
| `--keep-folder` | leave the folder in `/data` (expect duplicate files on the next import) |
| `--delete-folder` | delete the folder instead of quarantining it (requires `--yes`) |
| `--no-backup` | skip the database/archives backup that runs before `--apply` |
| `--apply` | actually change things (default: report only) |

- Stop the container first: a playlist that is `queued`/`running`, or still has an open job,
  is **refused** – the other requested playlists are processed anyway.
- Exit codes: `0` all good, `1` something was refused or failed, `2` usage/database error.
- The backup lands in `/backup` (`app-*.db` + `archives-*.tar.gz`, same rotation as the
  nightly run) and is the only undo besides the quarantined folder.
- A folder whose database row is already gone is found by its `… [<playlist_id>]` suffix and
  cleaned up as well.
- This is the single documented exception to the *never delete* rule: it is manual, offline,
  reports first and backs up first (`SPEC.md` §15). The runtime itself still never removes
  anything.

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

Optionally, a successful image push can notify your own endpoint: add the repository secrets `WEBHOOK_URL` and `WEBHOOK_TOKEN`. The build workflow then sends a `POST` with an `Authorization: Bearer <token>` header and waits (up to 10 minutes) for the response; the step fails unless the reply reports `summary.updated > 0`. Without the secrets nothing is sent.

See [SPEC.md](SPEC.md) for the specification (behaviour, the source of truth), [PLAN.md](PLAN.md) for status and the decision log and [AGENTS.md](AGENTS.md) for the working rules plus the map that says which kind of content belongs in which file.

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
├── export_common.py         Shared file placement, reports and CLI helpers of both exporters
├── ta_export.py               Standalone TubeArchivist export (python -m app.ta_export)
├── ts_export.py               Standalone TubeSync export, reads its SQLite read-only (python -m app.ts_export)
├── ta_import.py               Offline import of an exported tree into the database (python -m app.ta_import)
└── static/                  index.html, app.js, style.css (vanilla, no build step)
migrations/                  Alembic schema migrations
tests/                       pytest suite; fixtures/fake_ytdlp.py is the network-free stub
.github/workflows/           test.yml (ruff + pytest), build.yml (weekly amd64 image)
renovate.json                weekly dependency update schedule
SPEC.md / PLAN.md / AGENTS.md  specification, status + decisions, working rules
```

## 📜 License

Licensed under the [MIT License](LICENSE) - Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
