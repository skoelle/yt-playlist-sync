# SPEC: yt-playlist-sync

Version 1.1 (Stand Umsetzung) | Repo-Name: `yt-playlist-sync` | Öffentliches Repo, Lizenz MIT

## 1. Zweck

Ein selbst gehosteter Dienst, der die **öffentlichen Playlists eines YouTube-Kanals** stündlich erkennt und per `yt-dlp` als Video auf einen NAS-Share herunterlädt. Playlists, deren Titel das Schlüsselwort `setlist` enthalten ("Oneshot"), werden nur einmal vollständig geladen. Alle anderen ("Sync") werden einmal pro Nacht inkrementell synchronisiert. Eine kleine Web UI zeigt Status, Sync-Playlists und Oneshot-Playlists.

## 2. Ziele und Nicht-Ziele

### Ziele
- Kein Google-Login, keine OAuth-Secrets: nur öffentliche Playlists eines Kanals.
- Stündliche Discovery, neue Playlists werden sofort heruntergeladen.
- Nächtlicher Sync der Sync-Playlists, nie Löschen von Dateien.
- Genau ein Download-Job zur selben Zeit (keine Parallelität).
- Web UI mit 3 Tabs, Updates per JavaScript (`fetch`), kein HTML-Reload.
- Image per GitHub Action gebaut (nur `linux/amd64`, nur Tag `latest`) und nach GHCR gepusht.
- Betrieb als Docker Container auf `docker-host-nas`, Ziel ist ein NAS-Share.
- yt-dlp bleibt aktuell (Auto-Update im Container plus wöchentlicher Image-Rebuild).
- `DRY_RUN`-Modus zum gefahrlosen Testen.

### Nicht-Ziele
- Keine Audio-Konvertierung, keine Änderung an YouTube-Playlists.
- Keine Medienserver-Anbindung (Plex, Jellyfin), die Dateien sind aber kompatibel.
- Kein Login in der App (kommt später über Authelia davor).
- Kein Löschen von Dateien, Archive-Einträgen oder Playlists.
- Keine mehreren Accounts oder Kanäle.

## 3. Begriffe

| Begriff | Bedeutung |
|---|---|
| Discovery | Abruf der Playlist-Liste des Kanals |
| Sync-Playlist | Titel enthält NICHT das Schlüsselwort, wird nachts synchronisiert |
| Oneshot-Playlist | Titel enthält das Schlüsselwort (case-insensitive), wird nur einmal komplett geladen |
| Archive | yt-dlp `--download-archive` Datei pro Playlist |
| Job | Ein einzelner yt-dlp Lauf für eine Playlist |

## 4. Architektur

Ein Container, ein Python-Prozess, eingebaute Job-Queue mit genau einem Worker.

```
 Browser  --->  FastAPI (REST + statische UI)
                Scheduler (APScheduler): Discovery, Nachtsync, yt-dlp Update
                Job-Queue (1 Worker) ---> yt-dlp Subprozess (+ ffmpeg, deno)
                DB (SQLite, optional MariaDB) in /config
                Downloads nach /data (NAS-Share)
```

| Bereich | Wahl | Begründung |
|---|---|---|
| Sprache | Python 3.12 | yt-dlp ist Python, kleines Image |
| Web | FastAPI + Uvicorn | Async, einfache JSON-API |
| UI | HTML + Vanilla JS + CSS | Kein Build-Step, Polling per `fetch` |
| Scheduler | APScheduler 3.x | In-Process, Zeitzone konfigurierbar |
| DB-Zugriff | SQLAlchemy 2.x + Alembic | DB austauschbar per `DATABASE_URL` |
| DB Default | SQLite (`/config/app.db`) | Keine Zusatzabhängigkeit, ein Writer |

### 4.1 SQLite oder MariaDB?

Die Last ist minimal (ein Writer, wenige hundert Zeilen). SQLite ist dafür robuster und braucht keinen zweiten Dienst. MariaDB lohnt erst bei mehreren Instanzen oder wenn andere Tools die Daten abfragen sollen.

- Default: SQLite (WAL). `/config` muss ein lokales Volume sein, **nicht** NFS.
- Der Code nutzt nur SQLAlchemy. `DATABASE_URL=mysql+pymysql://user:pass@mariadb:3306/ytsync` funktioniert ohne Codeänderung (`pymysql` ist im Image).

## 5. Konfiguration (ENV)

| Variable | Default | Beschreibung |
|---|---|---|
| `YOUTUBE_CHANNEL` | (Pflicht) | Handle (`@beispielkanal`), Channel-ID (`UC...`) oder URL |
| `ONESHOT_KEYWORD` | `setlist` | Schlüsselwort im Titel, case-insensitive |
| `DISCOVERY_CRON` | `0 * * * *` | Stündlich |
| `SYNC_CRON` | `0 3 * * *` | Nachtsync |
| `YTDLP_UPDATE_CRON` | `30 2 * * *` | yt-dlp Update |
| `TZ` | `Europe/Berlin` | Zeitzone |
| `DATA_DIR` / `CONFIG_DIR` | `/data` / `/config` | Pfade im Container |
| `DATABASE_URL` | SQLite in `/config` | SQLAlchemy URL |
| `PUID` / `PGID` / `UMASK` | `1000` / `1000` / `0022` | Dateirechte |
| `SLEEP_MIN` / `SLEEP_MAX` | `3` / `10` | Pause zwischen Videos in Sekunden |
| `YTDLP_EXTRA_ARGS` | leer | Zusätzliche yt-dlp Argumente |
| `YTDLP_BIN` | `yt-dlp` | Programm (Tests nutzen einen Stub) |
| `HC_DISCOVERY_URL` / `HC_SYNC_URL` | leer | Healthchecks Ping-URLs |
| `DRY_RUN` | `0` | `1` = nur auflisten und simulieren |
| `LOG_LEVEL` / `LOG_RETENTION_DAYS` | `INFO` / `30` | Logging, Aufräumen alter Job-Logs (nie Videos) |

Es gibt keine YouTube-Secrets. Ping-URLs gehören in die lokale `.env`, nicht ins Repo.

## 6. Funktionale Anforderungen

### 6.1 Discovery (stündlich)
1. Ping `HC_DISCOVERY_URL/start`.
2. Liste holen: `yt-dlp --flat-playlist -J "https://www.youtube.com/<handle>/playlists"`.
3. Pro Playlist `playlist_id`, `title`, optional `item_count`.
4. Abgleich mit der DB:
   - **Neu:** anlegen, Typ bestimmen, Job sofort einreihen.
   - **Bekannt:** Titel und Zähler aktualisieren, Ordnername bleibt. Playlists im Status `new` (z. B. nach Dry-Run) werden erneut eingereiht.
   - **Verschwunden:** `remote_status = removed`, nichts löschen. Liefert YouTube eine leere Liste, obwohl Playlists bekannt sind, gilt der Lauf als Fehler.
5. Ping Erfolg bzw. `/fail`.

### 6.2 Typbestimmung
`oneshot`, wenn `ONESHOT_KEYWORD.lower()` in `title.lower()` steht, sonst `sync`. Der Typ wird einmalig beim ersten Erkennen festgelegt, in der UI per Button umstellbar.

### 6.3 Download-Job

```bash
yt-dlp \
  --ignore-errors --no-abort-on-error \
  --download-archive "/config/archives/<playlist_id>.txt" \
  -f "bv*+ba/b" --merge-output-format mp4/mkv \
  --convert-thumbnails jpg \
  --embed-thumbnail --embed-metadata --embed-chapters \
  --write-info-json --write-description --write-thumbnail \
  --write-subs --sub-langs "all,-live_chat" \
  --min-sleep-interval 3 --max-sleep-interval 10 \
  --newline --no-colors --progress-template "download:YTPS|%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s" \
  -o "/data/<Ordner>/%(playlist_index)02d - %(title).150B [%(id)s].%(ext)s" \
  "https://www.youtube.com/playlist?list=<playlist_id>"
```

- `bv*+ba/b` = beste Qualität ohne Auflösungsgrenze.
- `mp4/mkv`: yt-dlp nimmt mp4, sonst mkv. Beides funktioniert mit Plex und Jellyfin, eine Entscheidung ist nicht nötig.
- Untertitel nur manuell hochgeladene. `--write-auto-subs` mit `all` wird nicht genutzt (viele übersetzte Spuren führen schnell zu HTTP 429). Über `YTDLP_EXTRA_ARGS` ergänzbar.
- Ordnername: Playlist-Titel (bereinigt) plus `[playlist_id]`, beim ersten Download in `folder_name` gespeichert.
- `DRY_RUN=1` hängt `--simulate` an: keine Dateien, keine Archive-Einträge.
- Job-Logs: `/config/logs/<job_id>.log`.

### 6.4 Oneshot-Logik
- Erster Lauf lädt komplett.
- **`done`**, wenn jedes Video im Archive steht oder dauerhaft unavailable ist (privat, gelöscht, Alters- oder Mitglieder-Beschränkung). Diese zählen als `skipped`.
- **`failed`**, wenn Videos fehlen, die nicht dauerhaft unavailable sind. Die UI zeigt Fehlerzahl, Meldung und **Erneut versuchen** (lädt nur das Fehlende).
- Bei `done` wird die Playlist nie wieder automatisch angefasst. Kein Auto-Retry bei `failed`.
- **Full Re-Run** prüft eine fertige Oneshot erneut gegen das Archive und lädt nur Fehlendes. Nichts wird gelöscht.

### 6.5 Sync-Logik (nachts)
- `SYNC_CRON` reiht für jede Sync-Playlist (nicht `removed`, nicht `ignored`) einen Job ein.
- Das Archive sorgt dafür, dass nur neue Videos geladen werden.
- Ping `HC_SYNC_URL/start`, nach allen Jobs Erfolg oder `/fail`.
- Fehlgeschlagene Sync-Playlists werden in der nächsten Nacht erneut versucht.

### 6.6 Queue
- Genau ein Worker. Priorität: manuell/retry/full_rerun (0) > discovery (1) > nightly (2).
- Pro Playlist maximal ein offener Job.
- Beim Start werden `running`-Jobs auf `interrupted` gesetzt und neu eingereiht.
- Beim Beenden: SIGTERM an yt-dlp, nach 30 s SIGKILL. `.part` Dateien werden fortgesetzt.
- HTTP 429 oder Bot-Check: Job bricht ab (`failed`), Queue pausiert 30 Minuten.

### 6.7 yt-dlp Updates
- Beim Start (nach 10 s) und täglich: `pip install --upgrade --target /config/ytdlp-lib yt-dlp`. Der yt-dlp Prozess bekommt `PYTHONPATH=/config/ytdlp-lib`. Bei Fehlern bleibt die Image-Version.
- Das tägliche Update wartet bis zu 3 Stunden auf eine leere Queue.
- Die Action baut das Image wöchentlich ohne Cache neu. Die UI zeigt die yt-dlp Version.

### 6.8 Dateirechte
Der Entrypoint legt einen Benutzer mit `PUID`/`PGID` an und startet per `gosu`. Ist `/data` nicht beschreibbar, zeigt die UI ein rotes Banner und Downloads pausieren.

## 7. Datenmodell

**`playlists`:** `id`, `playlist_id` (unique), `title`, `type` (sync/oneshot), `folder_name`, `remote_item_count`, `downloaded_count`, `skipped_count`, `failed_count`, `size_bytes`, `state` (new, queued, running, done, failed, idle), `remote_status` (active/removed), `ignored`, `first_seen_at`, `last_seen_at`, `first_downloaded_at`, `completed_at` (Oneshot: "Datum des Herunterladens"), `last_sync_at`.

**`jobs`:** `id`, `playlist_id` (FK), `trigger` (discovery, nightly, manual, retry, full_rerun), `status` (queued, running, success, failed, interrupted, cancelled), `priority`, `queued_at`, `started_at`, `finished_at`, `current_item`, `progress_percent`, `items_new`, `items_skipped`, `items_failed`, `exit_code`, `error_summary`, `log_path`.

**`runs`:** `id`, `kind` (discovery/sync), `started_at`, `finished_at`, `status`, `message`.

`new` bedeutet "noch nie real heruntergeladen" (auch nach Dry-Run). Alle Zeitstempel sind UTC.

## 8. API

Unter `/api`, JSON. `{id}` ist die interne Playlist-ID.

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/status` | Laufender Job, Queue, letzte/nächste Läufe, yt-dlp Version, Schreibtest, freier Platz |
| GET | `/api/playlists?type=sync\|oneshot` | Playlists mit Zählern, Größe, letztem Job |
| GET | `/api/jobs?limit=50` | Jobhistorie |
| GET | `/api/jobs/{id}/log?offset=N` | Log ab Byte-Offset |
| POST | `/api/discovery/run`, `/api/sync/run` | Manuell starten |
| POST | `/api/playlists/{id}/run` | Jetzt synchronisieren |
| POST | `/api/playlists/{id}/retry` | Erneut versuchen (409 wenn nicht `failed`) |
| POST | `/api/playlists/{id}/rerun` | Full Re-Run |
| POST | `/api/playlists/{id}/ignore` | `{"ignored": true}` |
| POST | `/api/playlists/{id}/type` | `{"type": "sync"}` |
| POST | `/api/jobs/{id}/cancel` | Job abbrechen |
| GET | `/healthz` | Liveness |

## 9. Web UI

Single Page ohne Framework. `fetch` alle 5 Sekunden (nur bei sichtbarem Browser-Tab), Zeilen werden per Schlüssel verglichen und nur bei Änderung aktualisiert. Kein Seiten-Reload, Scrollposition und geöffnetes Log bleiben erhalten. Tabs über den URL-Hash.

- **Status:** aktueller Job (Fortschritt, Geschwindigkeit, ETA, Log-Panel, Abbrechen), Zeitpläne mit Buttons, System (Kanal, yt-dlp, Platz, Queue), letzte Jobs, Banner (DRY RUN, `/data` nicht beschreibbar, fehlgeschlagene Oneshots, Queue pausiert).
- **Sync-Playlists:** Titel, Videos, Größe, letzter und nächster Sync, Status, Aktionen (Jetzt syncen, Ignorieren, Als Oneshot markieren).
- **Oneshot-Playlists:** nach Download-Datum sortiert (sortierbar), Songs inkl. übersprungen/fehlgeschlagen, Größe, Dauer, Status, Aktionen (Erneut versuchen, Full Re-Run, Log, Als Sync markieren), Summenzeile.
- Dark Mode, relative Zeiten mit Tooltip, responsive.

## 10. Nicht-funktionale Anforderungen

- Robust gegen Neustarts (Archive plus `.part` Resume).
- Rate-Limit-Schutz durch Pausen, keine Parallelität, Queue-Pause.
- Image-Ziel unter 500 MB, Leerlauf-RAM unter 150 MB.
- Läuft als Nicht-Root, kein Docker-Socket, Pfade aus Titeln werden bereinigt.
- Tests laufen gegen einen yt-dlp-Stub ohne Netzwerk.

## 11. Build und Deployment

- `test.yml`: `ruff check` und `pytest -q` (Pull Requests, Pushes außerhalb `main`, wiederverwendbar).
- `build.yml`: Push auf `main`, manuell und wöchentlich (Montag 04:17 UTC, ohne Cache). Ruft zuerst die Tests auf, baut `linux/amd64` und pusht **nur `latest`** nach `ghcr.io/<owner>/yt-playlist-sync`.
- GitHub deaktiviert geplante Workflows nach 60 Tagen ohne Repo-Aktivität.
- Das GHCR-Package ist anfangs privat und muss einmalig auf "public" gestellt werden.
- Deployment per `docker-compose.example.yml` (nur Platzhalter), Updates über Watchtower.

## 12. Abnahmekriterien

1. Alle öffentlichen Playlists des Kanals erscheinen nach dem Start in der UI.
2. Titel mit "setlist" (beliebige Schreibweise) landen bei Oneshot, alle anderen bei Sync.
3. Neue Playlists werden spätestens zur nächsten vollen Stunde erkannt und sofort geladen.
4. Eine fertige Oneshot wird nie wieder automatisch angefasst.
5. Bei Fehlern steht die Oneshot auf `failed`, "Erneut versuchen" lädt nur Fehlendes.
6. Nie laufen zwei yt-dlp Prozesse gleichzeitig.
7. Der Nachtsync lädt nur Neues und meldet per Healthchecks Start, Erfolg oder Fehler.
8. Die UI aktualisiert ohne Seiten-Reload.
9. Nach `docker restart` mitten im Download geht es ohne doppelte Dateien weiter.
10. Es wird nie eine Datei oder ein Archive-Eintrag gelöscht.
11. Die Action baut ein amd64 Image, pusht `latest` und läuft wöchentlich ohne Cache.
12. Die UI zeigt die yt-dlp Version.

## 13. Offene Punkte

- Echter Kanal-Handle kommt aus der lokalen `.env`.
- Alters- oder Mitglieder-Videos sind ohne Login nicht ladbar (`skipped`). Optional später: `cookies.txt`.
- Bei YouTube-Bot-Erkennung helfen größere Sleep-Zeiten oder Cookies.
- MariaDB ist nur vorbereitet.

## 14. Änderungen gegenüber v1.0

- Repo öffentlich, MIT, README auf Englisch. Image-Tags nur `latest`.
- Container-Format `mp4/mkv` statt fest mp4.
- Neu: `DRY_RUN`, `YTDLP_BIN`, `UMASK`, Spalte `jobs.priority`. `queue.py` heißt `jobqueue.py`.
- Nur manuelle Untertitel, Thumbnails als JPG.
- Full Re-Run löscht nichts und prüft nur gegen das Archive.
- yt-dlp-Update über `pip --target /config/ytdlp-lib`.
- Alembic-Migration `0001` legt das Schema über `Base.metadata.create_all` an.
