# PLAN: yt-playlist-sync

Umsetzungsplan mit Status. Grundlage ist `SPEC.md`. Abweichungen stehen im Entscheidungslog am Ende und in SPEC Abschnitt 14.

## Umsetzungsstatus

- **Abgeschlossen:** alle Phasen abgenommen (0 bis 6), `pytest -q` = **51 grün**, `ruff check .` sauber, `node --check` ok, CI-Lauf inklusive.
- **Offene Tests (niedrige Prio):** Healthchecks-Mock, Cron-Berechnung/Zeitumstellung, Docker-Laufzeittests.

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

Abnahme: Abhängigkeiten installierbar, `ruff check .` und `pytest -q` grün, Test-Workflow grün. **Erfüllt (48 Tests grün, build.yml lief bis zum Push).**

## Phase 1: Config, Datenbank, Healthz

- [x] `app/config.py` (alle ENV, Validierung von Cron, Zeitzone, Sleep)
- [x] `app/db.py` (Engine, SQLite WAL, Session-Scope, Alembic-Aufruf)
- [x] `app/models.py` (`playlists`, `jobs`, `runs`)
- [x] Alembic `0001_initial`
- [x] `app/main.py` mit Lifespan und `/healthz`
- [x] `tests/test_config.py`

Abnahme: Start legt `app.db` an, `/healthz` liefert 200. **Erfüllt (Container läuft produktiv, Healthcheck grün).**

## Phase 2: Discovery und Typbestimmung

- [x] `app/ytdlp.py` Listing, URL-Bildung, Parser
- [x] `app/paths.py` Sanitizing und Typbestimmung
- [x] `app/discovery.py` (neu, bekannt, removed, `runs`, Schutz vor leerer Liste)
- [x] `tests/fixtures/fake_ytdlp.py`
- [x] Tests: Typbestimmung, Sanitizing, Discovery mit DB (grün)

Abnahme: manuell gegen echten Kanal mit `DRY_RUN=1`. **Teilweise.**

## Phase 3: Download-Runner und Queue

- [x] Kommandoaufbau, Ausgabeparser, Abschlusskriterium (`ytdlp.py`)
- [x] `app/runner.py` (Subprozess, Abbruch, Rate-Limit)
- [x] `app/jobqueue.py` (ein Worker, Prioritäten, Recovery, Cancel, Rate-Limit-Pause)
- [x] Oneshot-Regeln (`done`, `failed`, nie wieder automatisch)
- [x] Runner-Tests: Erfolg, zweiter Lauf, Teilfehler und Retry, unavailable, Dry-Run, Rate-Limit, Abbruch, Listing-Fehler (grün)
- [x] Queue-Tests: Oneshot nicht erneut, Retry, Priorität, Cancel (grün)
- [x] Test "nie zwei Jobs gleichzeitig" (Stub mit `slow`, nie mehr als ein `running`)
- [x] Neustart-Simulation (`recover()`: interrupted → requeued, States, kein doppelter Lauf)

Abnahme: manuell mit echter Test-Playlist. **Teilweise.**

## Phase 4: Scheduler und Healthchecks

- [x] `app/healthchecks.py`
- [x] `app/scheduler.py` (Discovery, Nachtsync, yt-dlp-Update, Startlauf, Log-Cleanup)
- [x] Nachtliches DB-Backup 0:30 (`app/backup.py`, sqlite3-Backup-API, `BACKUP_DIR`/`BACKUP_KEEP`)
- [x] Download-Archive im Nachtbackup (`backup_archives`: tar.gz von `archives/`, eigene Rotation, Integritätscheck durch Einlesen)
- [x] Discovery-Skip bei lauter Queue (`discovery_job(force=False)`: überspringt, solange Jobs queued/running, HC success `skipped: queue busy`; manueller Button mit `force=True` läuft immer)

Abnahme: Discovery läuft per Cron, `runs` korrekt. **Erfüllt (stündliche Discovery im laufenden Container).**

## Phase 5: REST-API und Web UI

- [x] `app/api.py` (alle Endpunkte aus SPEC 8)
- [x] `index.html`, `app.js`, `style.css` (3 Tabs, Polling, kein Reload, Log-Panel, Sortierung)
- [x] Detailseite (`GET /api/playlists/{id}`, `/files`, Hash `#playlist-{id}`, Titel-Links, globales Log-Panel)
- [x] Detailseite: Cover + Videogalerie (`read_video_entries` mit info.json-Parse-Cache, `/thumb`-Endpunkt)
- [x] Lokaler Player: `/video`-Stream (Range) + Lichtbox mit Playlist-Navigation
- [x] Technische Video-Metadaten in der Galerie (Auflösung, Größe, Codecs, Bitrate aus der `.info.json`, Helper `_resolution`/`_codec`/`_bitrate`)
- [x] `tests/test_api.py` (läuft: 48 Tests grün, inkls. Detailseite, `/thumb`, `/video`-Stream mit Range)
- [x] Test Log-Offset (Volltext, Nachschub, past-end, 422, `finished`)

Abnahme: im Browser prüfen (Stub oder `DRY_RUN`). **Erfüllt (UI manuell geprüft: Detailseite, Player, Metadaten).**

## Phase 6: Docker-Image, GitHub Action, README

- [x] `Dockerfile`, `docker-entrypoint.sh`
- [x] `docker-compose.example.yml`, `.env.example` (nur Platzhalter)
- [x] `build.yml` (amd64, nur `latest`, wöchentlich ohne Cache, ruft vorher die Tests auf)
- [x] `README.md` (Englisch)
- [x] `docker build` (build.yml lief, Image steht auf GHCR)
- [x] Laufzeitchecks im Container (läuft >2 h produktiv: yt-dlp, ffmpeg, Nicht-Root, `PUID`/`PGID`)
- [x] Erster Workflow-Lauf, GHCR-Package auf "public" gestellt

Abnahme: Image `latest` auf GHCR, ohne Login pullbar. **Erfüllt (public).**

## Definition of Done

- [x] Phasen 0 bis 6 vollständig abgenommen
- [x] Image `latest` auf GHCR, öffentlich, amd64
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
- DB-Backup über die `sqlite3`-Backup-API statt `cp`: WAL-Mode macht reine Dateikopien unzuverlässig; die API liefert einen konsistenten Snapshot ohne `-wal`-Sidecar. Rotation mit glob-Namensmuster, nur eigene `app-*.db`-Dateien.
- Das Nachtbackup sichert zusätzlich `/config/archives/` als `archives-*.tar.gz`: `/data` deckt der Nutzer über den NAS-Snapshot ab, aber die Archive sind winzig, nicht regenerierbar und ohne sie würde yt-dlp beim nächsten Lauf jedes Video neu prüfen (Rate-Limit-Risiko) bzw. Full Re-Run alles als fehlend sehen. Rotation je Namensmuster getrennt (`app-*.db` und `archives-*.tar.gz`), MariaDB betrifft nur den DB-Teil. `logs/` und `ytdlp-lib/` bleiben bewusst draußen (nicht kritisch bzw. wächst neu).
- Compose-Beispiel zeigt die echte öffentliche Image-URL `ghcr.io/skoelle/yt-playlist-sync:latest` und Übergabe von `ONESHOT_KEYWORD` per Interpolation (wie `YOUTUBE_CHANNEL`), nicht mehr hartkodiert; README-Raw-URL entsprechend korrigiert.
- `renovate.json` ergänzt: `config:recommended`, Schedule „before 6am on Monday", Gruppen für GitHub Actions (automerge), Docker/Docker Compose (manuell), Python dependencies (automerge).
- `.moonweb.yml` für die Projektübersicht (category `code`, subcategory „Archiving Tools").
- Discovery-Skip: Der stündliche Lauf startet kein zweites yt-dlp, solange die Queue nicht leer ist (`queue.is_idle()` deckt auch die 429-Pause ab). Kein `runs`-Eintrag beim Skip (Statuswert `"skipped"` hieße CheckConstraint-/Schema-Änderung – Log + HC-Message reichen); Healthcheck bekommt success `skipped: queue busy`, damit er nicht „late" meldet. Der manuelle Button behält `force=True`, weil er explizitem Nutzerwunsch folgt.
- yt-dlp-Update-Fix: Kommando von `yt-dlp` auf `yt-dlp[default]` geändert – `yt-dlp-ejs` (JS-Challenge-Solver) hängt nur am `default`-Extra und ist exakt gepinnt, lag daher nie im Target und die Env-Version lief mit. Danach `yt-dlp --rm-cache-dir`, wenn sich die wirksame yt-dlp-Version geändert hat (nicht am pip-Output gemessen: `pip --target` meldet immer `Successfully installed`): der Cache `/config/.cache/yt-dlp` (HOME=`/config`) hält veraltete Signaturen/Client-IDs, die nach Updates die Extraktion brechen. Kommandobau als reine Helfer in `ytdlp.py` (`build_update_command`, `build_cache_clear_command`) testbar gemacht; Fix per Scratch-Run verifiziert (ejs landet im Target).
- HTTP-403-Behandlung: Weder `_RATELIMIT` (429) noch `_PERMANENT` matchen 403, yt-dlp lief also vorher mit `--ignore-errors` durch die ganze Playlist und jede weitere 403-Anfrage verschlimmerte die Sperre. Jetzt eigener Flag `forbidden` (`parse_line`, `is_forbidden`), Runner bricht beim ersten 403 ab (auch beim Listing), Queue pausiert 30 min wie bei 429, Playlist wird sofort `failed` (`_finalize` bei `success=False`), und `JobQueue.on_forbidden` → `scheduler.schedule_update_after_403` stößt während der Pause den yt-dlp-Update an (Lock serialisiert gegen den Tages-Update, Cache-Clear greift bei Versionswechsel automatisch). Nur bei 403, nicht bei 429 (Entscheidung).
- Job-Gap mit Varianz: Zwischen zwei Jobs startete yt-dlp metronomisch (0 s, Prozess 2 exakt wenn Prozess 1 endet). Jetzt `JOB_GAP_MIN`/`JOB_GAP_MAX` (Default 1–10 s, `random.uniform`) vor dem zweiten und jedem weiteren Job; Flag `_gap_pending` wird nach jedem Job gesetzt und nur konsumiert, wenn wirklich ein Job ansteht – der erste Job nach Leerlauf startet sofort (manueller Button unverändert), Pause-/Writable-Checks verwerfen das Gap. Werte als int wie `SLEEP_*`, Validierung `0 ≤ min ≤ max`; Queue-Tests stellen 0/0, ein Timing-Test belegt den Abstand. (Entscheidung: Defaults aktiv statt Opt-in.)
- Discovery-Intervall statt Cron: `DISCOVERY_CRON = "0 * * * *"` war exakt :00 jeder Stunde, `startup_job` +10 s nach Start – vorhersehbar. Jetzt `DISCOVERY_INTERVAL_MIN`/`MAX` (Default 50/70 Minuten): `_schedule_discovery()` plant einen APScheduler-date-Job (`id="discovery"`, `replace_existing`) auf `now + uniform(50,70)` min, `_discovery_tick` plant zuerst den nächsten und läuft dann die Discovery – Läufe also gleichverteilt über den Tag, im Schnitt stündlich, ohne Wall-Clock-Anker, `discovery.next` zeigt die echte geplante Zeit. Start-Discovery (+10 s) bleibt sofort (Entscheidung). `DISCOVERY_CRON` entfällt und wird via `extra="ignore"` stillschweigend ignoriert (Breaking, SPEC §14); Sync-/Update-/Backup-Crons bleiben fix.
- Nachtlauf-Jitter: Der Sync-Batch startete täglich exakt 3:00:00 (schärfstes verbleibendes yt-dlp-Wanduhr-Muster), der erste Download nach Container-Start hingegen nur ~30 s am Start selbst (selten/unregelmäßig, bleibt laut Entscheidung sofort). Jetzt `SYNC_JITTER` (Default 30 min, `0` = aus): der Sync-Cron-Trigger bekommt APScheduler-`jitter` = `uniform(0, JITTER)` Nachlauf → Start 3:00–3:30, jeden Tag anders, `sync.next` zeigt die echte Zeit. Helfer `_cron_trigger()` baut den Trigger selbst, weil `from_crontab()` kein `jitter`-Kwarg hat; nur der Sync-Job nutzt ihn (Update/Backup/Cleanup: kein YouTube-Traffic).
- Sync-Tab-Parity: Summenzeile und Spalten-Sortierung waren nur für Oneshots implementiert (`#oneshot-summary`, `data-sort`, Click-Handler hart auf `renderOneshots()` verdrahtet, `state.sort` single) – reine Feature-Lücke, kein bewusstes Design; die API liefert beide Typen identisch. Jetzt: Erklärtext durch `#sync-summary` ersetzt (Entscheidung, wie Oneshot), `state.syncs` + eigenes `state.syncSort` (Default Titel aufsteigend wie die API-Reihenfolge, kein visueller Sprung), `syncSortValue`/`renderSyncs`, Click-Dispatch über `th.closest("table").id`. „Nächster Sync" bleibt unsortierbar (derselbe Scheduler-Wert in jeder Zeile). Wächter-Test `test_ui_sync_tab_has_summary_and_sort`.
