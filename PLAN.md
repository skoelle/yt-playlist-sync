# PLAN: yt-playlist-sync

Umsetzungsplan mit Status. Grundlage ist `SPEC.md`. Abweichungen stehen im Entscheidungslog am Ende und in SPEC Abschnitt 14.

## Umsetzungsstatus

- **Ausgeführt und grün (Sandbox ohne Internet):** 22 Tests für `paths`, `ytdlp` und `runner` gegen den yt-dlp-Stub. `app.js` besteht `node --check`. Alle Python-Dateien kompilieren.
- **Geschrieben, aber nicht ausgeführt** (FastAPI, SQLAlchemy, Alembic, APScheduler und pydantic-settings ließen sich in der Sandbox nicht installieren): `config`, `db`, `models`, Migration, `jobqueue`, `discovery`, `scheduler`, `healthchecks`, `api`, `main` sowie `tests/test_config.py`, `tests/test_queue_discovery.py`, `tests/test_api.py`. Diese Tests überspringen sich ohne die Pakete. Erster Lauf von `pytest -q` und `ruff check .` mit installierten Abhängigkeiten steht aus.
- **Nicht erledigt:** manuelle Tests gegen echtes YouTube, `docker build`, GitHub-Workflows, GHCR-Push, Package auf "public".
- **Nicht geschriebene Tests:** Healthchecks-Mock, Cron-Berechnung, "nie zwei Jobs gleichzeitig", Neustart-Simulation, Log-Offset, Docker-Laufzeittests.

## Regeln für den Agenten

1. Phasen der Reihe nach abarbeiten, Abnahme vor der nächsten Phase.
2. Pro Phase ein Commit mit Präfix `phase-N:`.
3. Öffentliches Repo: keine echten Kanal-Handles, Healthchecks-UUIDs, Hostnamen, IPs oder Pfade. `.env` ist in `.gitignore`.
4. Im Produktcode niemals Dateien oder Archive-Einträge löschen.
5. Tests nutzen den Stub `tests/fixtures/fake_ytdlp.py`, kein Netzwerk.
6. Linux-Beispiele mit `vim`, nie `nano`.
7. Code, Kommentare, README: Englisch. PLAN.md und SPEC.md: Deutsch.

## Phase 0: Repo-Grundgerüst

- [x] `LICENSE` (MIT), `.gitignore`, `.dockerignore`
- [x] `README.md` (Englisch)
- [x] `SPEC.md` an Änderungen angepasst
- [x] `requirements.txt`, `requirements-dev.txt`, `pyproject.toml`
- [x] `.github/workflows/test.yml`

Abnahme: Abhängigkeiten installierbar, `ruff check .` und `pytest -q` grün, Test-Workflow grün. **Offen.**

## Phase 1: Config, Datenbank, Healthz

- [x] `app/config.py` (alle ENV, Validierung von Cron, Zeitzone, Sleep)
- [x] `app/db.py` (Engine, SQLite WAL, Session-Scope, Alembic-Aufruf)
- [x] `app/models.py` (`playlists`, `jobs`, `runs`)
- [x] Alembic `0001_initial`
- [x] `app/main.py` mit Lifespan und `/healthz`
- [x] `tests/test_config.py`
- [ ] `tests/test_db.py` (Migration wird indirekt in `test_queue_discovery.py` geprüft)

Abnahme: Start legt `app.db` an, `/healthz` liefert 200. **Offen.**

## Phase 2: Discovery und Typbestimmung

- [x] `app/ytdlp.py` Listing, URL-Bildung, Parser
- [x] `app/paths.py` Sanitizing und Typbestimmung
- [x] `app/discovery.py` (neu, bekannt, removed, `runs`, Schutz vor leerer Liste)
- [x] `tests/fixtures/fake_ytdlp.py`
- [x] Tests: Typbestimmung, Sanitizing (grün); Discovery mit DB (nicht ausgeführt)

Abnahme: manuell gegen echten Kanal mit `DRY_RUN=1`. **Teilweise.**

## Phase 3: Download-Runner und Queue

- [x] Kommandoaufbau, Ausgabeparser, Abschlusskriterium (`ytdlp.py`)
- [x] `app/runner.py` (Subprozess, Abbruch, Rate-Limit)
- [x] `app/jobqueue.py` (ein Worker, Prioritäten, Recovery, Cancel, Rate-Limit-Pause)
- [x] Oneshot-Regeln (`done`, `failed`, nie wieder automatisch)
- [x] Runner-Tests: Erfolg, zweiter Lauf, Teilfehler und Retry, unavailable, Dry-Run, Rate-Limit, Abbruch, Listing-Fehler (grün)
- [x] Queue-Tests: Oneshot nicht erneut, Retry, Priorität, Cancel (nicht ausgeführt)
- [ ] Test "nie zwei Jobs gleichzeitig", Neustart-Simulation

Abnahme: manuell mit echter Test-Playlist. **Teilweise.**

## Phase 4: Scheduler und Healthchecks

- [x] `app/healthchecks.py`
- [x] `app/scheduler.py` (Discovery, Nachtsync, yt-dlp-Update, Startlauf, Log-Cleanup)
- [ ] Tests: Ping-Mock, Nachtsync-Logik, Cron mit Zeitumstellung

Abnahme: Discovery läuft per Cron, `runs` korrekt. **Offen.**

## Phase 5: REST-API und Web UI

- [x] `app/api.py` (alle Endpunkte aus SPEC 8)
- [x] `index.html`, `app.js`, `style.css` (3 Tabs, Polling, kein Reload, Log-Panel, Sortierung)
- [x] Detailseite (`GET /api/playlists/{id}`, `/files`, Hash `#playlist-{id}`, Titel-Links, globales Log-Panel)
- [x] `tests/test_api.py` (nicht ausgeführt)
- [ ] Test Log-Offset

Abnahme: im Browser prüfen (Stub oder `DRY_RUN`). **Offen.**

## Phase 6: Docker-Image, GitHub Action, README

- [x] `Dockerfile`, `docker-entrypoint.sh`
- [x] `docker-compose.example.yml`, `.env.example` (nur Platzhalter)
- [x] `build.yml` (amd64, nur `latest`, wöchentlich ohne Cache, ruft vorher die Tests auf)
- [x] `README.md` (Englisch)
- [ ] `docker build` und Laufzeitchecks (yt-dlp, ffmpeg, deno, Nicht-Root, `PUID`/`PGID`)
- [ ] Erster Workflow-Lauf, Package auf "public"

Abnahme: Image `latest` auf GHCR, ohne Login pullbar. **Offen.**

## Definition of Done

- [ ] Phasen 0 bis 6 vollständig abgenommen
- [ ] Image `latest` auf GHCR, öffentlich, amd64
- [x] README (Englisch), LICENSE (MIT), SPEC.md und PLAN.md im Repo
- [x] Keine privaten Daten im Repo (Platzhalter `example-user`, `@beispielkanal`)

## Entscheidungslog

- Format `mp4/mkv`: yt-dlp wählt selbst, Plex und Jellyfin unterstützen beide.
- `queue.py` heißt `jobqueue.py` (Namenskonflikt mit dem Standardmodul).
- Untertitel: nur manuelle, keine automatisch übersetzten (Rate-Limit-Risiko).
- yt-dlp-Updates per `pip --target /config/ytdlp-lib` und `PYTHONPATH`.
- Full Re-Run löscht nichts, prüft nur gegen das Archive.
- Dry-Run lässt Playlists im Status `new`, damit sie nach `DRY_RUN=0` wirklich geladen werden.
- Alembic-Migration `0001` nutzt `Base.metadata.create_all`.
- Der Test-Workflow ist wiederverwendbar (`workflow_call`), der Build ruft ihn vor dem Image-Build auf.
