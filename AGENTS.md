# AGENTS.md

## Projekt

`yt-playlist-sync`: selbst gehosteter Dienst (ein Container, ein Python-Prozess), der die öffentlichen Playlists eines YouTube-Kanals per `yt-dlp` nach `/data` lädt.

- **Sync-Playlists** (Titel ohne Schlüsselwort): nächtlicher Inkrementell-Sync über yt-dlp-Download-Archive.
- **Oneshot-Playlists** (Titel mit `ONESHOT_KEYWORD`, Default `setlist`, case-insensitive): genau ein vollständiger Lauf, danach nie wieder automatisch.
- Genau ein Download-Job zur selben Zeit, Pausen zwischen Videos, nie löschen.
- Kein Google-Login, keine API-Keys, kein Login in der App.

**Sprachregel:** Code, Kommentare, Tests, README auf Englisch. `SPEC.md`, `PLAN.md` und `MANUAL-PLAYLIST.md` auf Deutsch. Diese Datei: Deutsch. **UI-Texte (HTML/JS) sind Englisch** – kein i18n-Gerüst, keine deutschen Strings in `app/static/`; Wächter-Test `test_ui_is_english`.

## Architektur

```
FastAPI (app/main.py, Lifespan)
├── Scheduler (app/scheduler.py, APScheduler): Discovery als Intervall 50–70 min (Skip bei lauter Queue), Nachtsync, yt-dlp-Update, Log-Cleanup, Backups 0:30 (DB + Archive)
├── Job-Queue (app/jobqueue.py): genau 1 Worker, DB-backed, Prioritäten, Recovery, Cancel, 429-Pause
│     └── Runner (app/runner.py): 1 yt-dlp-Subprozess, ohne DB-Zugriff
│           └── ytdlp.py: URLs, Kommandobau, Output-Parser, Evaluate (rein, testbar)
├── Discovery (app/discovery.py): Abgleich Playlist-Tabelle, runs-Tabelle
├── REST API (app/api.py) + statische UI (app/static/, Vanilla JS, Polling ohne Reload)
└── DB (app/db.py + app/models.py): SQLAlchemy 2, Alembic, SQLite-WAL oder MariaDB
```

| Datei | Verantwortung |
|---|---|
| `app/config.py` | `Settings` (pydantic-settings), ENV-Validierung (Cron, TZ, Sleep), Pfade, `data_writable()` |
| `app/db.py` | Engine-Init (Global!), WAL-PRAGMAs, `session_scope()`, `utcnow()` (naives UTC), `migrate()` |
| `app/models.py` | `Playlist`, `Job`, `Run`, `PlaylistEntry` (Listing-Snapshot) + CheckConstraints über Tupel in `*_TYPES/STATUSES/TRIGGERS` |
| `app/backup.py` | `backup_database` (sqlite3-Backup-API statt Dateikopie), `backup_archives` (tar.gz von `archives/`), `sqlite_path`, Integritätschecks, Rotation `BACKUP_KEEP` je Namensmuster |
| `app/ytdlp.py` | `build_download_command`, `parse_line`, `evaluate`, `read_archive`/`append_archive` (append-only Download-Archive), `is_gone` (Musterliste: Playlist weg/privat/404), `channel_playlists_url`, `parse_video_listing` (Position/Dauer/Unavailable), `read_video_entries` (Galerie inkl. Tech-Metadaten, auch `.webm`), `build_update_command`/`build_cache_clear_command` (Update mit `[default]`, Cache-Clear) |
| `app/paths.py` | `sanitize_folder_name`, `is_oneshot` |
| `app/runner.py` | `RunParams`/`RunResult` (Felder `forbidden` für 403-Abbruch, `gone` für bestätigt verschwundene Playlist, `entries` = Listing-Snapshot), `ProcessHandle` (SIGTERM → 30 s → SIGKILL), Rate-Limit- und 403-Erkennung |
| `app/jobqueue.py` | `enqueue`/`cancel`/`recover`/`wait_for_jobs`, Prioritäten-Map `PRIORITY`, `_finalize` setzt Playlist-States und `_persist_entries` (Upsert `playlist_entries`, nie löschen), **Gone-Regel** (`r.gone` → `remote_status=removed` + `sync→oneshot` + `failed`, Counts unangetastet; Listing-Erfolg stellt nur `remote_status=active` wieder her, nie den Typ), Pause bei 429/403, `on_forbidden`-Callback, Job-Gap (`JOB_GAP_MIN`/`MAX`) zwischen zwei Jobs |
| `app/discovery.py` | `apply_discovery` (rein, nie löschen, **keine** `removed`-Markierung mehr – nicht gelistete Playlists bleiben unverändert; Existenz entscheidet der Sync-Lauf), `run_discovery` mit Leerlistenschutz |
| `app/scheduler.py` | Cron-Jobs (Sync inkl. `SYNC_JITTER`, Update, Cleanup, Backup) + Discovery als Intervall (`_schedule_discovery`/`_discovery_tick`, `DISCOVERY_INTERVAL_MIN/MAX`), Healthchecks-Pings, yt-dlp-Update via `pip --target /config/ytdlp-lib` (`yt-dlp[default]`) + `--rm-cache-dir` nach Versionsänderung, `schedule_update_after_403` (Lock gegen Tages-Update) |
| `app/healthchecks.py` | `ping(url, kind)` – Fehler werden nie weitergeworfen |
| `app/api.py` | Endpunkte aus SPEC §8, Serialisierer `job_dict`/`playlist_dict`, 409-Regeln, Media-Streaming `/thumb`+`/video` über `_safe_media_file` (Pfadsicherung), `/videos` mergt Dateien + `playlist_entries` + Archive zu Platzhalter-Zeilen (`_missing_entry`) |
| `app/main.py` | `create_app(settings, start_background)` – Tests nutzen `start_background=False` |
| `app/ta_export.py` | TubeArchivist-Export als Standalone-Skript (`python -m app.ta_export`), kein DB-Zugriff: `TaClient` (REST + Paginierung, `TA_API_HOST` Host-Header), `index_media`, `export()` schreibt Ordner/Sidecars/`manifest.json`/Archive, nie löschen, CLI + `.env`-Loader; läuft auch im Container auf der NAS (`/ta:ro` + `/export` gemountet; Hardlinks dort meist nicht möglich → `auto` fällt auf Kopie zurück); Platzierungs-/Report-Helfer importiert es aus `export_common` unter den alten Namen (`_place_file`, `_same_device`, …) |
| `app/export_common.py` | Gemeinsamer Kern beider Exporte: `MANIFEST_NAME`/`THUMB_EXTS`, `place_file` (`.part` → rename, `auto` Hardlink→Copy-Fallback, Größen-Check mit `repair`), `write_file`, `same_device`, `safe_id`, `check_mode`, Reports `PlaylistReport`/`ExportReport` + `print_report`, `load_dotenv`, `human` |
| `app/ts_export.py` | TubeSync-Export als Standalone-Skript (`python -m app.ts_export`): öffnet `config/db.sqlite3` **strikt read-only** (`file:…?mode=ro`), liest `sync_source`/`sync_media`/`sync_media_metadata` (`read_library`, Metadata über `media_id` sonst per `key`), schreibt dasselbe Layout/Manifest/Archive wie `ta_export` (Thumb-Sidecars, kein Cover, `description` bleibt in TubeSync); Reihenfolge `playlist_index` sonst `created`/`published`; CLI mit `TS_DB`/`TS_MEDIA_ROOT`/`TS_THUMBS_ROOT`/`TS_EXPORT_TARGET`, Exit 2 bei `TsError` |
| `app/ta_import.py` | Offline-Import eines exportierten Baums (`python -m app.ta_import`): `scan_manifests`/`select_manifests`/`resolve_folder` (rein), `apply_import` (Upsert `playlists` + `playlist_entries` in je einer Session pro Playlist, `reason`/`unavailable` unangetastet, nie löschen), Append-only-Merge der lokal vorhandenen Videos nach `config/archives`, alle Importe werden `oneshot` (neue Zeile: `done` nur bei vollständigen Dateien sonst `idle`; ein Update stuft vorhandene Zustände nie herunter), `--dry-run` schreibt nichts |
| `app/forget.py` | Wartungs-CLI (`python -m app.forget --playlist <id> …`, gestoppte App): `collect` (read-only Snapshot inkl. offener Jobs + Ordnerkandidaten über `[…<pid>]`-Suffix), `apply_forget` pro Playlist in einer Session, **Reihenfolge Ordner → Archive → DB** (`playlist_entries` → `jobs` → `playlists`, `foreign_keys=ON`), Ordner standardmäßig nach `<data>/.quarantine` (`--keep-folder`/`--delete-folder`+`--yes`), Guard bei `queued`/`running` oder offenem Job (Playlist `blocked`, die anderen laufen weiter), Backup (`backup_database`+`backup_archives`) nur mit `--apply`, **Default = Dry-Run**; die einzige dokumentierte Ausnahme von „nie löschen" (SPEC §15) |

Datenfluss-Regel: `runner.py` und `ytdlp.py` haben **keinen** DB-Zugriff. DB-Logik lebt in `jobqueue.py`, `discovery.py`, `api.py` – **Ausnahme:** `ta_import.py` und `forget.py` schreiben dieselben Tabellen offline (CLI, kein Laufzeitpfad, dokumentiert wie `backup.py`). `ts_export.py` liest fremde Daten (TubeSync-SQLite) strikt read-only.

## Befehle

```bash
# Setup (venv ist Pflicht, siehe globale Regeln)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# Tests und Lint (CI-Lauf: test.yml)
.venv/bin/pytest -q
.venv/bin/ruff check .

# Dev-Server (DRY_RUN verhindert echte Downloads; Port wie README/start-dev.sh)
.venv/bin/uvicorn app.main:app --reload --port 8048

# TubeArchivist-Export (Werte lokal in .env: TA_API_URL/TOKEN/HOST/TARGET/MEDIA_ROOT)
.venv/bin/python -m app.ta_export --dry-run --metadata-only

# TubeSync-Export (Werte lokal in .env: TS_DB/TS_MEDIA_ROOT/TS_THUMBS_ROOT/TS_EXPORT_TARGET)
.venv/bin/python -m app.ts_export --dry-run --metadata-only

# Import (App stoppen; --source zeigt auf den Export, Default: DATA_DIR)
.venv/bin/python -m app.ta_import --dry-run

# Forget (App stoppen; Default nur Report, erst --apply ändert etwas, siehe SPEC §15)
.venv/bin/python -m app.forget --playlist <pid>

# Derselbe Export im Container auf der NAS (lokal, ohne Netzübertragung; README-Abschnitt
# „Running the export inside the container"; im Image gibt es kein .env → alles über -e)
docker run --rm -e TA_API_URL=… -e TA_API_HOST=… -e TA_API_TOKEN=… \
  -e TA_MEDIA_ROOT=/ta -e TA_EXPORT_TARGET=/export \
  -v /pfad/tubearchivist:/ta:ro -v /pfad/export:/export \
  ghcr.io/skoelle/yt-playlist-sync:latest python -m app.ta_export --dry-run
```

Dev-Start mit lokalen Pfaden:

```bash
DATA_DIR=./.dev/data CONFIG_DIR=./.dev/config DRY_RUN=1 \
YOUTUBE_CHANNEL=@beispielkanal .venv/bin/uvicorn app.main:app --reload --port 8048
```

JS prüfen: `node --check app/static/app.js`. Docker-Build lokal: `docker build -t ytsync .`

## Tests

- **Immer den Stub nutzen**, nie echtes Netzwerk: `tests/fixtures/fake_ytdlp.py`, angebunden über die Fixture `stub` in `tests/conftest.py` (setzt `FAKE_YTDLP_DATA`, `stub.bin = "<python> <stub>"`, `stub.data_ref` + `stub.save()` für Fehlerfälle: `fail`, `slow`, `channel`, `playlists`, `listing_error` (Listing-Fehler je Playlist, z. B. die Gone-Meldung The playlist does not exist)).
- Datenbank- und API-Tests setzen `pytest.importorskip(...)` für die DB/Frame-Pakete und initialisieren die DB über `init_engine(c.db_url)` + `migrate(c.db_url)` in der `cfg`-Fixture. Die Engine ist global – jeder Test braucht eigene `tmp_path`-Pfade.
- API-Tests: `TestClient(create_app(cfg, start_background=False))` (kein Scheduler/Queue-Loop).
- Alle Pfade laufen über `tmp_path`, keine fixen Verzeichnisse.
- Bestehender Stand: **164 Tests grün** (`pytest -q`, ~27 s).

## Harte Regeln

Aus `SPEC.md` §2/§6 und `PLAN.md` „Regeln für den Agenten“:

1. **Nie löschen.** Keine Videos, Archive-Einträge, DB-Einträge oder Playlists entfernen. Ausnahmen: Job-Logdateien älter als `LOG_RETENTION_DAYS` (`scheduler.cleanup_logs`), Backup-Kopien jenseits der letzten `BACKUP_KEEP` (`scheduler.backup_run`), Temp-Dateien in Temporärverzeichnissen und die Wartungs-CLI `forget` (`app/forget.py` – nur manuell, nur gestoppte App, Default Dry-Run, vorher Backup, Ordner wandert nach `.quarantine`; SPEC §15).
2. **Ein Worker.** Niemals Parallelität von Downloads einführen (ein offener Job pro Playlist, `PRIORITY`-Map: manual/retry/full_rerun=0 > discovery=1 > nightly=2).
3. **Keine privaten Daten im Repo.** Nur Platzhalter (`@beispielkanal`, leere HC-URLs); die öffentliche Image-URL ist `ghcr.io/skoelle/yt-playlist-sync`. `.env` bleibt in `.gitignore`.
4. **Kein YouTube-Login, keine Secrets, kein API-Key.** `DATABASE_URL` und Healthchecks-URLs sind Deployment-Detail.
5. **Oneshot `done` bleibt `done`.** Nur `full_rerun` darf sie erneut anfassen; Retry nur bei `failed`.
6. **Timestamps UTC** (naiv, `db.utcnow()`), UI/Spec-Zeitzone nur für Cron und Anzeige.
7. **Rate-Limit-Schutz:** bei HTTP 429/Bot-Check oder HTTP 403 Job abbrechen und Queue 30 min pausieren (`RATE_LIMIT_PAUSE`); bei 403 zusätzlich den yt-dlp-Update-Check anstoßen. Sleep-Intervalle beibehalten.
8. **Kein Root im Container**, `PUID`/`PGID`/`gosu` respektieren, Pfade aus Titeln sanitizen (`paths.sanitize_folder_name`).
9. **Tests ohne Netzwerk**, Stub statt echtem yt-dlp; `YTDLP_BIN` ist die Austauschstelle.
10. Kein Build-Step für die UI: HTML/JS/CSS bleiben vanilla, Daten kommen per `fetch`, **kein `location.reload`** (davon gibt es einen Test).

## Code-Konventionen

- Python 3.12, `from __future__ import annotations` in Modulen mit moderner Typsyntax.
- Ruff: `line-length = 110`, Regeln `E, F, W, I`, Target `py312` (siehe `pyproject.toml`).
- Docstring in jeder Modul-Datei (1. Zeile), Namen englisch, Log-Message englisch.
- Logging: `logging.getLogger(__name__)`, FastAPI-App-Logger `yt-playlist-sync`.
- Exceptions aus der Laufzeit nie nach oben durchschlagen lassen, wo es um Verfügbarkeit geht (Healthchecks, Update, Persistenz): abfangen und loggen.
- API-Antworten als Plain-Dicts über die Helper `job_dict`/`playlist_dict`; Zeitstempel als ISO-String mit `Z`.
- DB-Zugriff immer über `with session_scope() as s:` (Commit am Ende, Rollback bei Fehler).
- Enum-artige Werte als Konstantentupel in `models.py`, CheckConstraints daraus ableiten.
- Keine neuen Abhängigkeiten ohne Bedarf; alles in `requirements.txt` (Runtime) vs. `requirements-dev.txt` (pytest, ruff).

## CI/CD

- `.github/workflows/test.yml`: `ruff check .` + `pytest -q`, Python 3.12 – bei Push außerhalb `main`, Pull Requests, und per `workflow_call`.
- `.github/workflows/build.yml`: ruft zuerst `test.yml`, dann Build `linux/amd64`, Tag nur `latest`, Push nach `ghcr.io/<owner>/yt-playlist-sync`, wöchentlich ohne Cache (Montag 04:17 UTC).
- `renovate.json`: Updates wöchentlich (vor 6 Uhr Montag), Minor/Patch automerge, Major mit Label `major-update`; Gruppen für Actions, Docker/Compose und Python.
- Nach einer Änderung immer `ruff check .` und `pytest -q` laufen lassen, bevor etwas als fertig gemeldet wird.

## Dokumentation und Stand

Jede Dokumentationsdatei hat einen festen Zweck. Inhalte dorthin legen, wo der Zweck
steht – das gilt für jede weitere Bearbeitung:

| Datei | Wofür | Rein | Niemals rein |
|---|---|---|---|
| `SPEC.md` | Quelle der Wahrheit für **Verhalten** (normativ, deutsch): Zweck, Anforderungen, ENV, API, Datenmodell, Abnahme, §14 Changelog (anfügen, nichts strichen), §15 Regel-Ausnahme (`forget`), §16/§17 Offline-Export/Import, technische Offene Punkte | jede Verhaltensänderung, dazu Migration und Test | How-to, Begründungen, Status/Checklisten |
| `README.md` | Nutzer & Betreiber (englisch): Quick start, Konfiguration, Volumes, Backup/Restore, Export/Import/Forgetting, Projektstruktur | Bedienanleitungen, ENV-Spalten, Deployment-Hinweise | interne Entscheidungen, Spezifikationstexte, TODOs |
| `AGENTS.md` | Regeln und Orientierung für die Arbeit am Code (diese Datei): Architektur, Befehle, Test-/Lint-Pflichten, harte Regeln, CI/CD, diese Dokumentationskarte | was vor dem ersten Eingriff gelten muss | Status, Changelog, Nutzeranleitung |
| `PLAN.md` | Status & Warum: Umsetzungsstand (Testzahlen), **Entscheidungslog** (append-only: Begründung, Alternative, Warum-gegen-um), priorisierte offene Punkte, Regeln für den Agenten | neue Entscheidungen, neue offene Punkte – bestehende Einträge nicht löschen | Verhalten (→ `SPEC.md`), How-to (→ `README.md`), erledigte Phasen-Checklisten |
| `MANUAL-PLAYLIST.md` | Offene Feature-Idee (noch nicht gebaut): manuelles Eintragen von Playlists – Entwurf mit bestätigten Entscheidungen, offenen Umsetzungspunkten, Risiken. Wenn fertig: Verhalten nach SPEC/README überführen, Datei zusammenschneiden | Entwürfe und offene Punkte, solange die Baustelle offen ist | erledigte Teile, veraltete Stände (der Import-/Export-Teil ist raus, steht in SPEC §14/§15 + README) |

Nicht ins Repo: persönliche Lauf- und Migrationsprotokolle (welche Playlist wann
übernommen wurde), echte Pfade, IDs oder Titel aus den eigenen Datenbeständen – sie
gehören ins Arbeitsprotokoll, nicht ins Werkzeug.

- Bekannter Ist-Stand beim Schreiben: `pytest -q` grün (164), `ruff check .` sauber, `node --check app/static/app.js` ok. Keine offenen Test-Punkte laut PLAN.md mehr.
- Verzeichnisse `@eaDir/` mit `*SynoEAStream`-Dateien sind Synology-Metadaten, kein Code – nicht bearbeiten, nicht als Quelltext behandeln.

## License

MIT License - Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
- Full text in `LICENSE`
- License headers in all source code files
