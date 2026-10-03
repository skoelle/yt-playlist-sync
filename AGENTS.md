# AGENTS.md

## Projekt

`yt-playlist-sync`: selbst gehosteter Dienst (ein Container, ein Python-Prozess), der die öffentlichen Playlists eines YouTube-Kanals per `yt-dlp` nach `/data` lädt.

- **Sync-Playlists** (Titel ohne Schlüsselwort): nächtlicher Inkrementell-Sync über yt-dlp-Download-Archive.
- **Oneshot-Playlists** (Titel mit `ONESHOT_KEYWORD`, Default `setlist`, case-insensitive): genau ein vollständiger Lauf, danach nie wieder automatisch.
- Genau ein Download-Job zur selben Zeit, Pausen zwischen Videos, nie löschen.
- Kein Google-Login, keine API-Keys, kein Login in der App.

**Sprachregel:** Code, Kommentare, Tests, README auf Englisch. `SPEC.md` und `PLAN.md` auf Deutsch. Diese Datei: Deutsch.

## Architektur

```
FastAPI (app/main.py, Lifespan)
├── Scheduler (app/scheduler.py, APScheduler): Discovery, Nachtsync, yt-dlp-Update, Log-Cleanup
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
| `app/models.py` | `Playlist`, `Job`, `Run` + CheckConstraints über Tupel in `*_TYPES/STATUSES/TRIGGERS` |
| `app/ytdlp.py` | `build_download_command`, `parse_line`, `evaluate`, `read_archive`, `channel_playlists_url` |
| `app/paths.py` | `sanitize_folder_name`, `is_oneshot` |
| `app/runner.py` | `RunParams`/`RunResult`, `ProcessHandle` (SIGTERM → 30 s → SIGKILL), Rate-Limit-Erkennung |
| `app/jobqueue.py` | `enqueue`/`cancel`/`recover`/`wait_for_jobs`, Prioritäten-Map `PRIORITY`, `_finalize` setzt Playlist-States |
| `app/discovery.py` | `apply_discovery` (rein, nie löschen), `run_discovery` mit Leerlistenschutz |
| `app/scheduler.py` | Cron-Jobs, Healthchecks-Pings, yt-dlp-Update via `pip --target /config/ytdlp-lib` |
| `app/healthchecks.py` | `ping(url, kind)` – Fehler werden nie weitergeworfen |
| `app/api.py` | Endpunkte aus SPEC §8, Serialisierer `job_dict`/`playlist_dict`, 409-Regeln |
| `app/main.py` | `create_app(settings, start_background)` – Tests nutzen `start_background=False` |

Datenfluss-Regel: `runner.py` und `ytdlp.py` haben **keinen** DB-Zugriff. DB-Logik lebt in `jobqueue.py`, `discovery.py`, `api.py`.

## Befehle

```bash
# Setup (venv ist Pflicht, siehe globale Regeln)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# Tests und Lint (CI-Lauf: test.yml)
.venv/bin/pytest -q
.venv/bin/ruff check .

# Dev-Server (DRY_RUN verhindert echte Downloads)
.venv/bin/uvicorn app.main:app --reload --port 8080
```

Dev-Start mit lokalen Pfaden:

```bash
DATA_DIR=./.dev/data CONFIG_DIR=./.dev/config DRY_RUN=1 \
YOUTUBE_CHANNEL=@beispielkanal .venv/bin/uvicorn app.main:app --reload --port 8080
```

JS prüfen: `node --check app/static/app.js`. Docker-Build lokal: `docker build -t ytsync .`

## Tests

- **Immer den Stub nutzen**, nie echtes Netzwerk: `tests/fixtures/fake_ytdlp.py`, angebunden über die Fixture `stub` in `tests/conftest.py` (setzt `FAKE_YTDLP_DATA`, `stub.bin = "<python> <stub>"`, `stub.data_ref` + `stub.save()` für Fehlerfälle: `fail`, `slow`, `channel`, `playlists`).
- Datenbank- und API-Tests setzen `pytest.importorskip(...)` für die DB/Frame-Pakete und initialisieren die DB über `init_engine(c.db_url)` + `migrate(c.db_url)` in der `cfg`-Fixture. Die Engine ist global – jeder Test braucht eigene `tmp_path`-Pfade.
- API-Tests: `TestClient(create_app(cfg, start_background=False))` (kein Scheduler/Queue-Loop).
- Alle Pfade laufen über `tmp_path`, keine fixen Verzeichnisse.
- Bestehender Stand: **34 Tests grün** (`pytest -q`, ~5 s).

## Harte Regeln

Aus `SPEC.md` §2/§6 und `PLAN.md` „Regeln für den Agenten“:

1. **Nie löschen.** Keine Videos, Archive-Einträge, DB-Einträge oder Playlists entfernen. Ausnahmen: Job-Logdateien älter als `LOG_RETENTION_DAYS` (`scheduler.cleanup_logs`) und Temp-Dateien in Temporärverzeichnissen.
2. **Ein Worker.** Niemals Parallelität von Downloads einführen (ein offener Job pro Playlist, `PRIORITY`-Map: manual/retry/full_rerun=0 > discovery=1 > nightly=2).
3. **Keine privaten Daten im Repo.** Nur Platzhalter (`@beispielkanal`, `example-user`, leere HC-URLs). `.env` bleibt in `.gitignore`.
4. **Kein YouTube-Login, keine Secrets, kein API-Key.** `DATABASE_URL` und Healthchecks-URLs sind Deployment-Detail.
5. **Oneshot `done` bleibt `done`.** Nur `full_rerun` darf sie erneut anfassen; Retry nur bei `failed`.
6. **Timestamps UTC** (naiv, `db.utcnow()`), UI/Spec-Zeitzone nur für Cron und Anzeige.
7. **Rate-Limit-Schutz:** bei HTTP 429/Bot-Check Job abbrechen und Queue 30 min pausieren (`RATE_LIMIT_PAUSE`), Sleep-Intervalle beibehalten.
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
- Nach einer Änderung immer `ruff check .` und `pytest -q` laufen lassen, bevor etwas als fertig gemeldet wird.

## Dokumentation und Stand

- `SPEC.md` = Spezifikation und Quelle der Wahrheit für Verhalten, ENV-Tabelle, API, Datenmodell. Bei Verhaltensänderung **immer** SPEC (und bei Bedarf README) mitpflegen.
- `PLAN.md` = Umsetzungsstatus, Phasen, Entscheidungslog. Offene Punkte dort fortschreiben statt bestehende Einträge löschen.
- `README.md` = Nutzerdoku (Englisch), Quick start, ENV-Tabelle, Volumes.
- Bekannter Ist-Stand beim Schreiben: `pytest -q` grün (34), `ruff check .` meldet **8× E501** (Zeilen > 110, 3× in `app/`, 5× in `tests/`) – beim nächsten Durchgang zuerst aufräumen.
- Offen laut PLAN.md: erster vollständiger CI-Lauf mit installierten Abhängigkeiten, `docker build`, manuelle Läufe gegen echtes YouTube, GHCR-Package auf „public“, diverse fehlende Tests (Healthchecks-Mock, Cron/Zeitumstellung, „nie zwei Jobs parallel“, Neustart, Log-Offset).
- Verzeichnisse `@eaDir/` mit `*SynoEAStream`-Dateien sind Synology-Metadaten, kein Code – nicht bearbeiten, nicht als Quelltext behandeln.

## License

MIT License - Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
- Full text in `LICENSE`
- License headers in all source code files
