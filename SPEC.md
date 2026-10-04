# SPEC: yt-playlist-sync

Version 1.1 (Stand Umsetzung) | Repo-Name: `yt-playlist-sync` | Öffentliches Repo, Lizenz MIT

## 1. Zweck

Ein selbst gehosteter Dienst, der die **öffentlichen Playlists eines YouTube-Kanals** alle 50–70 Minuten (im Schnitt stündlich) erkennt und per `yt-dlp` als Video auf einen NAS-Share herunterlädt. Playlists, deren Titel das Schlüsselwort `setlist` enthalten ("Oneshot"), werden nur einmal vollständig geladen. Alle anderen ("Sync") werden einmal pro Nacht inkrementell synchronisiert. Eine kleine Web UI zeigt Status, Sync-Playlists und Oneshot-Playlists.

## 2. Ziele und Nicht-Ziele

### Ziele
- Kein Google-Login, keine OAuth-Secrets: nur öffentliche Playlists eines Kanals.
- Discovery im Intervall von 50–70 Minuten (im Schnitt stündlich), neue Playlists werden sofort heruntergeladen.
- Nächtlicher Sync der Sync-Playlists, nie Löschen von Dateien.
- Genau ein Download-Job zur selben Zeit (keine Parallelität).
- Web UI mit 3 Tabs, Updates per JavaScript (`fetch`), kein HTML-Reload.
- Image per GitHub Action gebaut (nur `linux/amd64`, nur Tag `latest`) und nach GHCR gepusht.
- Betrieb als Docker Container mit lokalem Volume für `/config`, Ziel ist ein NAS-Share.
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
                Scheduler (APScheduler): Discovery, Nachtsync, yt-dlp Update, Log-Cleanup, Backups 0:30
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
| `ONESHOT_KEYWORD` | `setlist` | Komma-getrennte Schlüsselwörter im Titel, case-insensitive (z. B. `setlist,concert`) |
| `DISCOVERY_INTERVAL_MIN` / `DISCOVERY_INTERVAL_MAX` | `50` / `70` | Discovery-Intervall in Minuten (jeder Lauf zufällig in diesem Bereich) |
| `SYNC_CRON` | `0 3 * * *` | Nachtsync |
| `SYNC_JITTER` | `30` | Zufälliger Nachlauf nach `SYNC_CRON` in Minuten (`0` = aus) |
| `YTDLP_UPDATE_CRON` | `30 2 * * *` | yt-dlp Update |
| `TZ` | `Europe/Berlin` | Zeitzone |
| `DATA_DIR` / `CONFIG_DIR` | `/data` / `/config` | Pfade im Container |
| `DATABASE_URL` | SQLite in `/config` | SQLAlchemy URL |
| `PUID` / `PGID` / `UMASK` | `1000` / `1000` / `0022` | Dateirechte |
| `SLEEP_MIN` / `SLEEP_MAX` | `3` / `10` | Pause zwischen Videos in Sekunden |
| `JOB_GAP_MIN` / `JOB_GAP_MAX` | `1` / `10` | Zufällige Pause zwischen zwei Jobs (Playlists) in Sekunden |
| `YTDLP_EXTRA_ARGS` | leer | Zusätzliche yt-dlp Argumente |
| `YTDLP_BIN` | `yt-dlp` | Programm (Tests nutzen einen Stub) |
| `HC_DISCOVERY_URL` / `HC_SYNC_URL` | leer | Healthchecks Ping-URLs |
| `DRY_RUN` | `0` | `1` = nur auflisten und simulieren |
| `LOG_LEVEL` / `LOG_RETENTION_DAYS` | `INFO` / `30` | Logging, Aufräumen alter Job-Logs (nie Videos) |
| `BACKUP_DIR` / `BACKUP_KEEP` | `/backup` / `7` | Zielverzeichnis der nächtlichen Backups (DB + Download-Archive), Anzahl Kopien pro Typ (`0` = unbegrenzt) |

Es gibt keine YouTube-Secrets. Ping-URLs gehören in die lokale `.env`, nicht ins Repo.

## 6. Funktionale Anforderungen

### 6.1 Discovery (alle 50–70 Minuten)
- **Planung ohne Cron:** Beim Start plant der Scheduler die erste Laufzeit als `now + uniform(DISCOVERY_INTERVAL_MIN, DISCOVERY_INTERVAL_MAX)` Minuten; jeder Tick plant **vor** dem Lauf den nächsten (`_discovery_tick` → `_schedule_discovery`, date-Job `id="discovery"`), damit lange oder übersprungene Läufe die Kette nicht verschieben. Die Start-Discovery (+10 s nach Container-Start, `startup_job`) läuft sofort und zusätzlich; sie startet die Kette nicht. `GET /api/status` (`discovery.next`) zeigt den echten geplanten Zeitpunkt. `DISCOVERY_CRON` wird nicht mehr ausgewertet (kommt via `extra="ignore"` stillschweigend durch).
1. **Übersprungener Lauf:** Solange die Queue nicht leer ist (Jobs `queued` oder `running` – laufender Download, noch nicht abgearbeitete Jobs oder die 429-Pause), wird der automatische Lauf übersprungen: kein `runs`-Eintrag, Log-Meldung, Ping `HC_DISCOVERY_URL` success mit der Message `skipped: queue busy` (damit Healthchecks nicht „late" meldet). Der nächste Versuch ist der nächste geplante Lauf; nach einem Neustart mit requeued Jobs ebenso. Der manuelle Button (`POST /api/discovery/run`) überspringt **nicht**.
2. Ping `HC_DISCOVERY_URL/start`.
3. Liste holen: `yt-dlp --flat-playlist -J "https://www.youtube.com/<handle>/playlists"`.
4. Pro Playlist `playlist_id`, `title`, optional `item_count`.
5. Abgleich mit der DB:
   - **Neu:** anlegen, Typ bestimmen, Job sofort einreihen.
   - **Bekannt:** Titel und Zähler aktualisieren, Ordnername bleibt. Playlists im Status `new` (z. B. nach Dry-Run) werden erneut eingereiht.
   - **Verschwunden:** `remote_status = removed`, nichts löschen. Liefert YouTube eine leere Liste, obwohl Playlists bekannt sind, gilt der Lauf als Fehler.
6. Ping Erfolg bzw. `/fail`.

### 6.2 Typbestimmung
`oneshot`, wenn eines der Komma-getrennten `ONESHOT_KEYWORD`-Wörter case-insensitive in `title` steht, sonst `sync`. Der Typ wird einmalig beim ersten Erkennen festgelegt, in der UI per Button umstellbar. Eine spätere Änderung von `ONESHOT_KEYWORD` wirkt nur auf Playlists, die danach neu erkannt werden – bestehende behalten ihren Typ.

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
- **`failed`**, wenn Videos fehlen, die nicht dauerhaft unavailable sind. Die UI zeigt Fehlerzahl, Meldung und **Retry** (lädt nur das Fehlende).
- Bei `done` wird die Playlist nie wieder automatisch angefasst. Kein Auto-Retry bei `failed`.
- **Full Re-Run** prüft eine fertige Oneshot erneut gegen das Archive und lädt nur Fehlendes. Nichts wird gelöscht.

### 6.5 Sync-Logik (nachts)
- `SYNC_CRON` reiht für jede Sync-Playlist (nicht `removed`, nicht `ignored`) einen Job ein.
- **Jitter:** Der Lauf startet nicht exakt auf den Cron-Slot, sondern `random.uniform(0, SYNC_JITTER)` Minuten danach (APScheduler-Trigger-`jitter`, Default 30 → 3:00–3:30 Uhr, jeden Tag anders). `sync.next` in `GET /api/status` zeigt den verzögerten Zeitpunkt; `SYNC_JITTER=0` ergibt das alte Verhalten (exakter Slot).
- Das Archive sorgt dafür, dass nur neue Videos geladen werden.
- Ping `HC_SYNC_URL/start`, nach allen Jobs Erfolg oder `/fail`.
- Fehlgeschlagene Sync-Playlists werden in der nächsten Nacht erneut versucht.

### 6.6 Queue
- Genau ein Worker. Priorität: manuell/retry/full_rerun (0) > discovery (1) > nightly (2).
- Pro Playlist maximal ein offener Job.
- Beim Start werden `running`-Jobs auf `interrupted` gesetzt und neu eingereiht.
- Beim Beenden: SIGTERM an yt-dlp, nach 30 s SIGKILL. `.part` Dateien werden fortgesetzt.
- **Job-Gap:** Vor dem zweiten und jedem weiteren Job wartet der Worker eine zufällige Pause von `JOB_GAP_MIN`..`JOB_GAP_MAX` Sekunden (Default 1–10, `random.uniform`), damit zwei yt-dlp-Prozesse nicht metronomisch aufeinanderfolgen. Vor dem ersten Job nach Leerlauf gibt es kein Gap – ein manueller Start läuft sofort. Nach einer Rate-Limit-/403-Pause und bei nicht schreibbarem `DATA_DIR` wird das Gap verworfen (die Pause selbst reicht als Abstand).
- HTTP 429 oder Bot-Check: Job bricht ab (`failed`), Queue pausiert 30 Minuten.
- **HTTP 403** (z. B. `unable to download video data: HTTP Error 403: Forbidden`): Job bricht sofort ab statt durch die Playlist weiterzureichen – `failed`, Playlist `failed`, Queue pausiert 30 Minuten wie bei 429. Zusätzlich wird ein yt-dlp-Update-Check angestoßen (siehe 6.7). Auch ein 403 beim Auflisten der Playlist löst Abbruch und Pause aus. 403 gilt als temporär (nicht `permanent`): die Videos zählen als `missing`, Retry und nächster Nachtlauf bleiben möglich.

### 6.7 yt-dlp Updates
- Beim Start (nach 10 s) und täglich: `pip install --upgrade --target /config/ytdlp-lib "yt-dlp[default]"`. Das `default`-Extra ist nötig, weil `yt-dlp-ejs` (der JavaScript-Challenge-Solver) nur dort hängt und exakt gepinnt ist (`==`): passt die Version in der Env nicht mehr, installiert pip den passenden ejs mit ins Target. Der yt-dlp Prozess bekommt `PYTHONPATH=/config/ytdlp-lib`. Bei Fehlern bleibt die Image-Version.
- Wenn sich durch das Update die wirksame yt-dlp-Version geändert hat, läuft danach `yt-dlp --rm-cache-dir`. (Auf pip-Output wird nicht geprüft: `pip --target` installiert und meldet `Successfully installed` bei jedem Lauf.) Der Cache unter `/config/.cache/yt-dlp` (HOME des Container-Users ist `/config`) enthält gecachte Signaturen und Client-IDs, die nach einem Update veraltet sind und die Extraktion brechen können. Ohne Versionsänderung bleibt der Cache unangetastet.
- Das tägliche Update wartet bis zu 3 Stunden auf eine leere Queue.
- **Dritter Auslöser HTTP 403:** Nach einem wegen 403 abgebrochenen Job stößt der Scheduler einen Update-Check an (`schedule_update_after_403`). Er läuft nach dem Abbruch während der Queue-Pause, wartet bis auf den finalisierten Job und nutzt dieselben pip-/Versions-/Cache-Schritte wie der Start-Update. Tages-Update und 403-Update laufen über einen `asyncio.Lock` serialisiert, damit kein zwei pip-Läufe gleichzeitig ins gleiche Target schreiben.
- Die Action baut das Image wöchentlich ohne Cache neu. Die UI zeigt die yt-dlp Version.

### 6.8 Dateirechte
Der Entrypoint legt einen Benutzer mit `PUID`/`PGID` an und startet per `gosu`. Ist `/data` nicht beschreibbar, zeigt die UI ein rotes Banner und Downloads pausieren.

### 6.9 Backups (nächtlich 0:30)
- Täglich um `0:30` (Zeitzone = `TZ`) sichert der Scheduler zwei Dinge nach `BACKUP_DIR`, Dateinamen mit Zeitstempel `YYYYMMDD-HHMMSS` (Lokalzeit):
  - **Datenbank:** `app-YYYYMMDD-HHMMSS.db`. Umsetzung über die `sqlite3`-Backup-API der Stdlib, nicht per Dateikopie: bei laufendem Betrieb (WAL-Mode) liefert sie einen konsistenten Snapshot inklusive WAL-Inhalt, und die Kopie ist eine standalone DB ohne `-wal`-Sidecar. Geprüft per `PRAGMA integrity_check`; bei Fehlern wird geloggt und die Datei liegen gelassen.
  - **Download-Archive:** `archives-YYYYMMDD-HHMMSS.tar.gz` (tar.gz von `/config/archives/`, alle `<playlist_id>.txt`). Winzig, aber nicht regenerierbar: ohne Archive prüft yt-dlp beim nächsten Lauf jedes Video neu (langsam, Rate-Limit-Risiko) und ein Full Re-Run einer fertigen Oneshot sähe alles als fehlend. Geprüft durch vollständiges Einlesen des tar.gz; fehlt das Verzeichnis, wird der Lauf übersprungen und geloggt.
- Rotation: von den eigenen Dateien **je Namensmuster** (`app-*.db` und `archives-*.tar.gz`) bleiben die neuesten `BACKUP_KEEP` (Default 7, `0` = unbegrenzt), ältere Kopien werden gelöscht. Gelöscht werden ausschließlich diese Backup-Dateien – nie Videos, Archive oder DB-Einträge.
- Nur SQLite: bei einem `DATABASE_URL` auf MariaDB wird der DB-Teil übersprungen und geloggt (MariaDB-Backup läuft über `mysqldump`, siehe 4.1); der Archive-Teil läuft unabhängig davon.
- Jeder Lauf fängt Fehler ab (Verzeichnis nicht beschreibbar, Quelldatei fehlt): Fehler werden geloggt, der Dienst läuft weiter, kein Crash.
- **Restore:** Container stoppen, `app.db` durch die Backup-Datei ersetzen, danebenliegende alte `app.db-wal`/`app.db-shm` entfernen; Archive mit `tar -xzf archives-<Stempel>.tar.gz -C /config` entpacken (vorhandene Dateien werden nur überschrieben, nichts gelöscht), Container starten.

## 7. Datenmodell

**`playlists`:** `id`, `playlist_id` (unique), `title`, `type` (sync/oneshot), `folder_name`, `remote_item_count`, `downloaded_count`, `skipped_count`, `failed_count`, `size_bytes`, `state` (new, queued, running, done, failed, idle), `remote_status` (active/removed), `ignored`, `first_seen_at`, `last_seen_at`, `first_downloaded_at`, `completed_at` (Oneshot: "Datum des Herunterladens"), `last_sync_at`.

**`jobs`:** `id`, `playlist_id` (FK), `trigger` (discovery, nightly, manual, retry, full_rerun), `status` (queued, running, success, failed, interrupted, cancelled), `priority`, `queued_at`, `started_at`, `finished_at`, `current_item`, `progress_percent`, `items_new`, `items_skipped`, `items_failed`, `exit_code`, `error_summary`, `log_path`.

**`runs`:** `id`, `kind` (discovery/sync), `started_at`, `finished_at`, `status`, `message`.

**`playlist_entries`:** `id`, `playlist_id` (FK), `video_id` (unique je Playlist), `position`, `title`, `duration_s`, `unavailable` (Listing-Marker `[Private video]`/`[Deleted video]` oder permanenter Fehler), `reason` (letzte ERROR-Meldung, leer sobald das Video im Archive steht), `remote_present` (aus dem letzten Listing verschwanden → ausgeblendet), `last_seen_at`. Snapshot des Flat-Listings vom letzten Job-Lauf; Zeilen werden nie gelöscht, nur aktualisiert (SPEC 2/„Nicht-Ziele").

`new` bedeutet "noch nie real heruntergeladen" (auch nach Dry-Run). Alle Zeitstempel sind UTC.

## 8. API

Unter `/api`, JSON. `{id}` ist die interne Playlist-ID.

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/status` | Laufender Job, Queue, letzte/nächste Läufe, yt-dlp Version, Schreibtest, freier Platz |
| GET | `/api/playlists?type=sync\|oneshot` | Playlists mit Zählern, Größe, letztem Job |
| GET | `/api/playlists/{id}` | Detailseite: alle DB-Felder plus Job-Verlauf (letzte 25) |
| GET | `/api/playlists/{id}/files` | Dateien im Zielordner: Name, Größe, mtime, Summen nach Endung. Existiert der Ordner nicht, `exists: false`; Pfade außerhalb von `data_dir` werden ignoriert |
| GET | `/api/playlists/{id}/videos` | Videogalerie: Cover (`00 - …jpg`) und pro Video Titel, Dauer, Datum, Aufrufe sowie technische Daten aus der `.info.json` (Auflösung, Dateigröße, Video-/Audio-Codec mit Bitrate; Parse-Cache pro Datei), Summe der Dauern. Aus dem Listing-Snapshot (`playlist_entries`) werden nicht heruntergeladene Videos als Platzhalter-Zeilen in Playlist-Reihenfolge eingefügt: `missing: true`, `file: null`, `status` (`unavailable`, `failed`, `archived`, `pending`) und `reason`; erscheinen auch, bevor der Zielordner existiert |
| GET | `/api/playlists/{id}/thumb?file=NAME` | Thumbnail aus dem Zielordner als Bild; nur `jpg/jpeg/png/webp`, kein `/` oder `..`, sonst 404 |
| GET | `/api/playlists/{id}/video?file=NAME` | Videodatei streamen (`mkv/mp4/webm`) mit Range-Support für Spulen; gleiche Pfadsicherheit wie `thumb` |
| GET | `/api/jobs?limit=50` | Jobhistorie |
| GET | `/api/jobs/{id}/log?offset=N` | Log ab Byte-Offset |
| POST | `/api/discovery/run`, `/api/sync/run` | Manuell starten |
| POST | `/api/playlists/{id}/run` | Jetzt synchronisieren |
| POST | `/api/playlists/{id}/retry` | Retry (409 wenn nicht `failed`) |
| POST | `/api/playlists/{id}/rerun` | Full Re-Run |
| POST | `/api/playlists/{id}/ignore` | `{"ignored": true}` |
| POST | `/api/playlists/{id}/type` | `{"type": "sync"}` |
| POST | `/api/jobs/{id}/cancel` | Job abbrechen |
| GET | `/healthz` | Liveness |

## 9. Web UI

Single Page ohne Framework. **Die UI-Texte sind Englisch** (Buttons, Spalten-Header, Badges, Leerzustände, Dialoge, Datums- und Zahlenformate `en-US`); SPEC und dieses Dokument bleiben Deutsch. `fetch` alle 5 Sekunden (nur bei sichtbarem Browser-Tab), Zeilen werden per Schlüssel verglichen und nur bei Änderung aktualisiert. Kein Seiten-Reload, Scrollposition und geöffnetes Log bleiben erhalten. Tabs über den URL-Hash, Detailseiten als `#playlist-{id}`.

- **Status:** aktueller Job (Fortschritt, Geschwindigkeit, ETA, **Cancel**), **Schedules** mit Buttons, System (Channel, yt-dlp, Free space, Queue), **Recent jobs**, Banner (DRY RUN, `/data` nicht beschreibbar, fehlgeschlagene Oneshots, Queue pausiert). Das Log-Panel ist global und öffnet von jeder Ansicht. Die Schedules-Karte ist pro Job aufgeteilt: Überschrift (**Discovery** / **Nightly sync**), darunter `next:` mit absoluter Uhrzeit und relativer Zeit, darunter `last:` mit Status und relativer Zeit, und die Run-Message (`n playlists, n new, …`) in eigener kleiner Zeile darunter.
- **Sync-Playlists:** Summenzeile (`n Sync-Playlists | Videos | Size` in `#sync-summary`, Erklärtext aus v1.0 ersetzt), Titel (verlinkt auf die Detailseite, kleiner ↗-Link nach YouTube), Videos, Size, letzter und nächster Sync, Status, Aktionen (**Sync now**, **Ignore**, **Mark as oneshot**). Sortierbar nach Titel (Standard, aufsteigend), Videos, Size, letztem Sync und Status, eigenes Sortier-Zustand je Tab; „Next sync" ist nicht sortierbar, weil dort derselbe Scheduler-Zeitpunkt in jeder Zeile steht.
- **Oneshot-Playlists:** standardmäßig nach Titel sortiert (aufsteigend, wie die Sync-Playlists; eigener Sortier-Zustand je Tab), zusätzlich sortierbar nach Download-Datum, Songs, Size, Duration und Status, Songs inkl. übersprungen/fehlgeschlagen (`not available`/`failed`), Size, Duration, Status, Aktionen (**Retry**, **Full Re-Run**, **Log**, **Mark as sync**), Summenzeile. `GET /api/playlists?type=oneshot` liefert ebenfalls Titelreihenfolge.
- **Detailseite:** Kopf mit Zurück-Link, Playlist-Cover, Titel, Typ-/Status-Badges, Stats-Zeile (Videos, Gesamtdauer, Size) und kleinem YouTube-Link; Aktionen (je nach Status). Videogalerie im YouTube-Listenstil: Thumbnail mit Dauer-Badge, Titel, Kanal · Datum · Views und darunter eine Meta-Zeile mit technischen Daten aus der `.info.json` (Auflösung, Dateigröße, Video-/Audio-Codec mit Bitrate), vorhandene Sidecar-Dateien als Chips – live gepollt. Fehlende Videos erscheinen als **Platzhalter-Zeile** an ihrer Playlist-Position: gebrochenes Video-Symbol statt Thumbnail, Badge (`unavailable`, `failed`, `not downloaded`, `file missing`), der bekannte Grund (Fehlermeldung) als Textzeile, Titel verlinkt auf YouTube statt auf den lokalen Player, der Player überspringt diese Zeilen. Klick auf Thumbnail oder Titel öffnet den lokalen Player (Lichtbox, `<video>` auf das Streaming-Endpunkt): Vor/Zurück-Buttons, beim Ende läuft das nächste Video, ESC oder Klick auf den Hintergrund schließt, YouTube-Link als Fallback. Darunter alle DB-Felder als KV-Tabelle, die rohe Dateiliste zugeklappt in „All files" und der Job-Verlauf der Playlist mit Log-Buttons.
- Dark Mode, relative Zeiten mit Tooltip, responsive.
- **Responsive (Breakpoint 768 px):** unterhalb davon klappt die UI auf Mobile um. Die Nav zeigt `Sync`/`Oneshot` ohne `-Playlists`-Suffix (Links brechen nie mitten im Wort um; `nav` ist Flex mit Gap). Die vier Datentabellen (Sync, Oneshot, Recent jobs, Job history) werden aus Tabelle zu Karten: Titel als Kartenkopf, darunter Label/Wert-Paare pro Zelle (`data-label`), Aktionen als Button-Reihe mit Trennlinie; die Job-ID-Spalte entfällt. Die Spalten-Kopfzeile wird zur Reihe aus Sortier-Chips (nur `th[data-sort]`), Sortierung bleibt nutzbar. Kein horizontales Scrollen mehr. Über dem Breakpoint bleibt das klassische Tabellenlayout unverändert.

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
- Das GHCR-Package ist public; `docker pull` funktioniert ohne Login.
- `renovate.json`: Dependency-Updates montags vor 6 Uhr, Minor/Patch automerge, Major mit Label; Gruppen für Actions, Docker/Compose und Python.
- Deployment per `docker-compose.example.yml` (echte Image-URL `ghcr.io/skoelle/yt-playlist-sync:latest`, Beispiel-Pfade; Pfade und Ping-URLs lokal anpassen), Volumes `/config`, `/data`, `/backup`, Updates über Watchtower.

## 12. Abnahmekriterien

1. Alle öffentlichen Playlists des Kanals erscheinen nach dem Start in der UI.
2. Titel mit "setlist" (beliebige Schreibweise) landen bei Oneshot, alle anderen bei Sync.
3. Neue Playlists werden spätestens zur nächsten vollen Stunde erkannt und sofort geladen.
4. Eine fertige Oneshot wird nie wieder automatisch angefasst.
5. Bei Fehlern steht die Oneshot auf `failed`, "Retry" lädt nur Fehlendes.
6. Nie laufen zwei yt-dlp Prozesse gleichzeitig.
7. Der Nachtsync lädt nur Neues und meldet per Healthchecks Start, Erfolg oder Fehler.
8. Die UI aktualisiert ohne Seiten-Reload.
9. Nach `docker restart` mitten im Download geht es ohne doppelte Dateien weiter.
10. Es wird nie eine Datei oder ein Archive-Eintrag gelöscht.
11. Die Action baut ein amd64 Image, pusht `latest` und läuft wöchentlich ohne Cache.
12. Die UI zeigt die yt-dlp Version.
13. Die Detailseite zeigt Cover und Videogalerie; Klick auf ein Video spielt die lokale Datei (Hintergrund-Klick oder ESC schließt wieder).
14. Nachts um 0:30 liegt in `BACKUP_DIR` ein per Integritätscheck geprüftes DB-Backup und ein geprüftes tar.gz der Download-Archive, alte Kopien rotieren je Typ nach `BACKUP_KEEP`.

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
- Neu: Playlist-Detailseite mit Cover, Video-Galerie und lokalem Player (`GET /videos`, `/thumb`, `/video`), Klick auf Hintergrund/ESC/Buttons schließt die Lichtbox.
- Neu: Nächtliches SQLite-Backup um 0:30 nach `BACKUP_DIR` mit Rotation `BACKUP_KEEP` (siehe 6.9).
- Neu: Das nächtliche Backup enthält zusätzlich die Download-Archive als `archives-*.tar.gz` (siehe 6.9).
- Neu: Die stündliche Discovery wird übersprungen, solange die Queue nicht leer ist; der manuelle Button überspringt nicht (siehe 6.1).
- Neu: HTTP 403 bricht Jobs sofort ab (Pause wie 429, Playlist `failed`) und stößt einen yt-dlp-Update-Check an (siehe 6.6/6.7).
- Neu: Zufällige Pause von `JOB_GAP_MIN`..`JOB_GAP_MAX` Sekunden zwischen zwei Jobs, damit yt-dlp-Starts nicht metronomisch sind (siehe 6.6).
- Neu: Discovery-Intervall statt Cron: `DISCOVERY_INTERVAL_MIN`/`MAX` (Default 50/70 Minuten, zufällig pro Lauf); `DISCOVERY_CRON` wird nicht mehr ausgewertet (siehe 6.1).
- Neu: Nachtlauf mit Jitter: `SYNC_JITTER` (Default 30 Minuten) verzögert den Sync-Slot zufällig, `sync.next` zeigt die echte Zeit (siehe 6.5).
- Neu: Die Web-UI ist einsprachig Englisch (Buttons, Spalten-Header, Badges, Leerzustände, Dialoge, `en-US`-Formate) – vorher deutsch bei bereits englischem Backend; kein i18n-Gerüst (siehe 9).
- Neu: Platzhalter für fehlende Videos in der Videogalerie – Listing-Snapshot je Playlist in `playlist_entries` (Migration `0002`, beim Finalisieren jedes Jobs aktualisiert), `GET /videos` liefert Platzhalter-Zeilen mit Status und Grund, UI zeigt sie mit gebrochenem Video-Symbol (siehe 7, 8, 9).
