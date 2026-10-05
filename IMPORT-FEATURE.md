# IMPORT-FEATURE: Manuelles Eintragen + Import aus TubeSync/TubeArchivist

Arbeitsdokument für das Feature "manuelle Playlists eintragen" und "fehlende Videos aus
externen Archiven vervollständigen". Kein Quelltext, sondern Planung + Recherche-Stand.

**Arbeitsmodus:** der Reihe nach, Phase für Phase. Nach jeder Phase:
`.venv/bin/ruff check .` und `.venv/bin/pytest -q` laufen lassen, dann `[x]` setzen.

---

## 1. Ziel

1. **Manuelles Eintragen beliebiger Playlists** (inkl. *unlisted*, kein YouTube-Login,
   kein API-Key): URL in die UI eintippen → Zeile in der DB → einmal eingeben, nie
   automatisch wieder gelöscht.
2. **Vervollständigung fehlender Videos** aus einer bestehenden Self-Hosted-Lösung
   (TubeSync / TubeArchivist): Videos, die dort schon liegen, in unsere Playlist-Ordner
   übernehmen und im Download-Archive vermerken, statt sie neu über YouTube zu laden.

Nicht Teil des Features: automatischer Import, Löschen irgendetwas, YouTube-Login.

---

## 2. Bestätigte Entscheidungen

Aus dem Fragebogen (nicht mehr neu verhandeln):

| # | Frage | Entscheidung |
|---|---|---|
| 1 | Ausführung des Imports | **Nur manuell (Button je Playlist)** – kein Cron, kein Auto-Mitlauf in der Nightly-Sync |
| 2 | Formular fürs Eintragen | **Beide Tabs** (Sync + Oneshot), Typ folgt dem aktiven Tab |
| 3 | Import-Modus | **`copy`** (`shutil.copy2`) als Default, `hardlink` nur optional via ENV |
| 4 | Zugriff auf Quelldaten | **Dateisystem-Scan + TubeArchivist REST-API**; TubeSync bleibt FS-only |
| 5 | Playlists selbst | **Unlisted reicht**, kein Login nötig – validiert wird per `yt-dlp --flat-playlist -J` |
| 6 | Ausführung | **Job mit neuem Trigger `import`**, Priorität 0, eigene CheckConstraint-Migration |
| 7 | Metadaten/Membership | TA-API für Titel, Position und Playlist-Zugehörigkeit; **immer mit FS-Scan als Fallback** |

Projektregeln, die gelten (AGENTS.md / SPEC):

- **Harte Regel 1 – nie löschen.** Import kopiert nur und hängt Archive-Zeilen an.
  Quelle (`IMPORT_ROOTS`) ist nur lesend. Ausnahmen bleiben Logs/Backups-Rotation.
- **Harte Regel 2 – ein Worker.** Der Import läuft *im selben* Queue-Worker, kein
  zweiter paralleler Prozess.
- **Harte Regel 4 – kein Login/Secrets/API-Key** für YouTube. `TA_API_TOKEN` ist ein
  Deployment-Detail wie `DATABASE_URL` (ENV, nicht im Repo).
- **Harte Regel 5 – Oneshot `done` bleibt `done`.** Import darf done-Oneshots nicht
  anfassen; nur `full_rerun` öffnet sie wieder. Wird in SPEC + Test festgehalten.
- **Harte Regel 9 – Tests ohne Netzwerk.** TA-API wird über `httpx.MockTransport`
  gefaked, FS-Quellen über `tmp_path`-Bäume, yt-dlp über den Stub.
- **Sprachregel:** Code/Kommentare/Tests/README Englisch, `SPEC.md`/`PLAN.md` Deutsch,
  **UI-Texte Englisch** (Wächter `tests/test_api.py::test_ui_is_english`).

---

## 3. Recherche-Stand (Befund, Stand Okt 2026)

### 3.1 Was im Code schon da ist

| Fundstelle | Befund |
|---|---|
| `app/api.py:111` | Nur `GET /api/playlists` – **kein POST**. `playlist_dict()` ab Zeile 41, `_get_playlist` ab 60, `_enqueue_or_409` ab 66. |
| `app/api.py:229` | `POST /playlists/{pid}/type` zeigt das Body-Pydomänen-Muster (`TypeBody`). |
| `app/discovery.py:62` | `if pid not in seen and pl.remote_status != "removed": pl.remote_status = "removed"` → **markiert manuelle Playlists als entfernt**. Genau diese Stelle braucht den Guard. |
| `app/discovery.py:44` | Neue Zeilen bekommen `remote_status="active"` – Manuelles muss das auch. |
| `app/scheduler.py:120` | Nightly-Sync filtert `type=="sync" AND ignored==False AND remote_status=="active"` → manuelle Sync-Playlists laufen mit, manuelle Oneshots nicht (richtig so). |
| `app/jobqueue.py:25` | `PRIORITY = {"manual":0,"retry":0,"full_rerun":0,"discovery":1,"nightly":2}` – `import` braucht Eintrag `0`. |
| `app/jobqueue.py:86` | `enqueue`: Auto-Trigger-Skip bei `ignored`/`removed`; `PRIORITY[trigger]` → unbekannter Trigger wirft `KeyError`. |
| `app/jobqueue.py:99` | `cancel()`: queued → hart abbrechen; running → nur über `self._handle` (Subprozess). Import hat keinen Prozess → braucht eigene Cancellation. |
| `app/jobqueue.py:217` | `_run_job`: baut `RunParams`, ruft `run_playlist()`, dann `_finalize()`. **Hier kommt der Import-Zweig hin.** |
| `app/jobqueue.py:304` | `_finalize(job_id, playlist_type, RunResult, size)`: setzt Counts, `pl.state`, bei Oneshot-Erfolg `done` + `completed_at`. **Kann für Import wiederverwendet werden**, wenn der Import ein `RunResult` liefert. |
| `app/ytdlp.py:261` | `evaluate()`: `unavailable` und `archived` zählen denselben Eintrag ggf. doppelt → Summe ≠ `total`. Fix siehe Phase 3. |
| `app/ytdlp.py:249` | `read_archive()` liest `youtube <id>`-Zeilen. Es gibt **kein** `append_archive()` – das fehlt. |
| `app/ytdlp.py:291` | `_STEM = ^(\d+) - (.+) \[([^\][]+)\]$` – Zielschema der eigenen Dateien. |
| `app/ytdlp.py:166` | `list_playlist_entries()` → `--flat-playlist -J` auf einer Playlist-URL. Genau das validiert die manuelle URL. |
| `app/paths.py` | `sanitize_folder_name()` (Ordner), `is_oneshot()`. **Datei-Namen-Sanitizer fehlt.** |
| `app/runner.py:82` | Archive-Pfad-Konstruktion, `RunParams`/`RunResult`-Definition (Zeile 17/31). |
| `app/config.py` | `Settings`-Muster: Feld + `field_validator`/`model_validator` + Property für Pfade. |
| `app/models.py:12` | `JOB_TRIGGERS = ("discovery","nightly","manual","retry","full_rerun")` → `import` ergänzen. |
| `app/db.py:52` | `migrate()` = `alembic upgrade head`. |
| `migrations/versions/0001_initial.py` | `upgrade()` = **`Base.metadata.create_all()` mit den *aktuellen* Models** → frische DB enthält künftige Spalten sofort. Daraus folgt der Existenz-Guard in `0002`. |
| `requirements.txt:6` | `httpx>=0.27` ist schon Runtime-Dependency (`app/healthchecks.py:21` nutzt es async). Für den Import (läuft in `asyncio.to_thread`) den **sync** `httpx.Client` nutzen. |
| `app/static/index.html:95/106` | Tabs `tab-sync` / `tab-oneshot` mit `<table class="ct">` – da kommt das Formular hin. |
| `app/static/app.js:501` | `async function act(action, el)` – `dispatch`, `api()` ab Zeile 73. Neue Actions einhängen. |
| `app/static/app.js:175/306` | Sync-Row- bzw. Oneshot-Row-Actions – da kommt der `Import`-Button hin. |

### 3.2 TubeArchivist-API (belegt, aber nicht gegen Live-Instanz geprüft)

- Auth: `Authorization: Token <TA_API_TOKEN>` (Header, kein Query-Param).
- Swagger je Instanz unter `/api/docs/`.
- `GET /api/playlist/` → `{"data": [ { "playlist_name": …, "playlist_url": …,
  "playlist_entries": [ {"youtube_id": …}, … ] } ]}`, Pagination via `?page=`.
  `playlist_url` enthält `list=<youtube_playlist_id>` → daüber Playlist-Matching.
- `GET /api/video/<video-id>/` → u. a. `media_url` (z. B. `/youtube/<channel_id>/<video_id>.mp4`),
  `title`, `duration`. Pfad relativ zum TA-Root → `TA_ROOT / media_url.lstrip("/")`.
- `GET /api/video/?playlist=<ta_playlist_id>` als Membership-Alternative (ungeprüft).

> **Risiko:** Shape stammt aus Doku + Community-Snippets, nicht aus einer laufenden
> Instanz. Deshalb **Architektur-Regel: API-Ergebnis ist Komfort, FS-Scan ist Garantie.**
> Jeder API-Fehlschlag (404, JSON-Fehler, Timeout) → still auf FS-Scan fallen, loggen,
> den Job nicht deshalb scheitern lassen.

### 3.3 TubeSync (FS-only, kein API)

- Layout: `…/youtube-dl/{channel}/{upload_date} - {title}-{youtube_id}.{ext}`
  (Video-ID als eigenes Suffix, in der Regel **ohne** eckige Klammern).
- Konsequenz: Der FS-Scan darf nicht nur nach `[<id>]` suchen, sondern nach
  `[<id>]`, `-<id>.`, `<id>.` und `<id>` als reines Suffix.
- Da TubeSync keine Membership-API hat, kommt die Playlist-Zugehörigkeit immer aus
  unserer eigenen Playlist-Listung (yt-dlp) bzw. der TA-API – nie aus TubeSync.

### 3.4 Archiv-/Dateikonventionen

- Archive-Zeile: `youtube <videoid>` (eine pro Zeile), Ziel `config_dir/archives/…`.
- Zielname: `NN - <Titel> [<videoid>].<ext>`, `NN` = Playlist-Index (2-stellig),
  Titel auf ~150 Byte gekürzt. Cover: `00 - …jpg` (`_find_cover` in `app/ytdlp.py:327`).
- Sidecars mit gleichem Stem werden mitkopiert: `.info.json`, `.jpg`, `.jpeg`, `.png`,
  `.webp`, `.description`.
- Existiert die Zieldatei schon → **nur** Archive-Zeile anhängen, nicht kopieren.

### 3.5 Testinfrastruktur

- Stub `tests/fixtures/fake_ytdlp.py`: `--flat-playlist` auf einer Playlist-URL liest
  `data["playlists"][pid]` → **unbekannte ID wirft `KeyError`**. Für den 400-Pfad von
  `POST /playlists` muss der Stub bei unbekannter ID sauber `exit 1` + stderr-Text
  machen (sonst bleibt das untestbar).
- Queue-Tests-Muster (`tests/test_queue_discovery.py:154`):
  `asyncio.run` mit `queue.start()` → `queue.wait_for_jobs([jid], poll=0.2)` → `queue.stop()`.
- API-Tests: `TestClient(create_app(cfg, start_background=False))`, DB-Init über
  `init_engine(c.db_url)` + `migrate(c.db_url)` in der `cfg`-Fixture.

---

## 3a. Phase 0 (umgesetzt): TubeArchivist-Export nach externem Layout

**Stand 2026-10-05.** Statt Phase 3 (Import-Trigger im Queue-Worker) wurde zuerst der
Export der TA-Daten ins eigene Ordnerformat gebaut – eigenständig nutzbar, ohne App-,
DB- oder SPEC-Änderung. Die Darstellung/der DB-Import folgen in einem späteren Schritt.

- **Umsetzung:** `app/ta_export.py` (Bibliothek, kein DB-Zugriff) + CLI
  `python -m app.ta_export`; Tests `tests/test_ta_export.py` (MockTransport, `tmp_path`).
- **Quelle:** TA-REST-API, gegen die Live-Instanz geprüft:
  `GET /api/playlist/` (48 Playlists, `playlist_id`, `playlist_name`, `playlist_entries`
  mit `youtube_id`/`title`/`uploader`/`idx`) und `GET /api/video/` (1211 Videos, volle
  Doku inkl. `media_url`, `streams`, `stats`, `vid_thumb_url`), beides seitig paginiert.
  **Host-Override nötig** (`TA_API_HOST`): dieses Deployment lehnt den Host der URL ab
  (`400 DisallowedHost`), `Host: localhost` funktioniert.
- **Pfad-Mapping:** API `media_url=/youtube/<ch>/<id>.mp4` → Platte `media/<ch>/<id>.mp4`;
  der Export indiziert `<media-root>/media/*` einmalig und matched über die Video-ID.
- **Ziel:** `TA_EXPORT_TARGET` (NFS-Share), Layout identisch zu yt-dlp
  (`sanitize_folder_name` / neu `sanitize_filename` / `NN - Titel [id].ext`,
  Cover `00 - … [<pid>].jpg`), Sidecars `.info.json` (generiert, yt-dlp-nah),
  `.description`, Thumb, pro Playlist `manifest.json` (Grundlage für den DB-Import,
  liefert u. a. den Playlist-Typ-Grund und alle Eintrags-IDs) und
  `<target>/archives/<pid>.txt` (append-only, nur bei vorhandener Mediendatei).
- **Flags:** `--metadata-only` (nur Metadata), `--dry-run` (nichts schreiben),
  `--mode auto|copy|hardlink` (Default `auto` = Hardlink-Versuch mit Copy-Fallback;
  das hiesige Synology-NFS antwortet auf `link()` mit `EPERM`, fällt also auf Kopie),
  `--playlist` (Teil-Lauf), `--no-archives`.
- **Atomares Schreiben:** Ziel wird erst als `<name>.part` geschrieben und dann per
  `os.replace` umbenannt – ein abgebrochener Lauf kann keine abgeschnittenen Videos
  unter dem finalen Namen hinterlassen. Zweitlauf vergleicht zusätzlich die Größe und
  repariert unvollständige Dateien (`repaired`/`rep` im Report).
- **Regeln:** nie löschen, nie überschreiben, idempotenter Zweitlauf; fehlende
  Mediendateien werden berichtet statt archiviert (Live-Dry-run: 111 von 1360 Einträgen).
- **Konfiguration nur lokal** in `.env` (in `.gitignore`): `TA_API_URL`, `TA_API_TOKEN`,
  `TA_API_HOST`, `TA_EXPORT_TARGET`, `TA_MEDIA_ROOT`; `.env.example` nur Platzhalter.
  Hostname, Token und Pfade nie committen.
- **Laufort Container auf der NAS:** vorgesehene Ausführung ist
  `docker run … python -m app.ta_export` (README „Running the export inside the container"):
  TA-Daten als `/ta:ro` und Export als `/export` lokal eingehängt, dadurch keine
  doppelte Netzübertragung und Hardlinks möglich (auf der Arbeitsplatte über NFS greift
  der Copy-Fallback). `--place-only` bewusst nicht gebaut.

Damit wird Phase 3 (Import-Trigger) vorerst zurückgestellt: sie wird erst dann wieder
relevant, wenn das Legacy-Layout in der App dargestellt bzw. in die DB übernommen wird.

---

## 4. Phasenplan

Jede Phase ist eigenständig abschließbar und muss `ruff` + `pytest` grün hinterlassen.

### Phase 1 — Fundament: Config, Models, Migration

- [ ] **`app/config.py`** – vier neue Felder:
  - `import_roots: str = ""` (Liste, getrennt durch `os.pathsep` bzw. `:`)
  - `import_mode: str = "copy"`
  - `ta_api_url: str = ""`
  - `ta_api_token: str = ""`
  - `field_validator("import_mode")`: nur `{"copy", "hardlink"}`, sonst `ValueError`
  - Property `import_root_paths() -> list[Path]` (leere Einträge filtern, `expanduser`)
  - Property `import_enabled() -> bool`: `bool(import_root_paths()) or ta_api_url != ""`
- [ ] **`app/models.py`**
  - `Playlist.manual: Mapped[bool] = mapped_column(Boolean, default=False)`
    (kein neues CheckConstraint nötig)
  - `JOB_TRIGGERS = (… , "import")`
- [ ] **`migrations/versions/0002_manual_and_import.py` (neu)**
  - **Existenz-Guard ist Pflicht** (siehe 3.1: `0001` erzeugt per `create_all` alles,
    was in den aktuellen Models steht):
    ```python
    from sqlalchemy import inspect as sa_inspect

    def upgrade() -> None:
        bind = op.get_bind()
        insp = sa_inspect(bind)
        if "manual" not in {c["name"] for c in insp.get_columns("playlists")}:
            op.add_column("playlists",
                          sa.Column("manual", sa.Boolean(), nullable=False,
                                    server_default=sa.false()))
        triggers = [c.get("name") for c in insp.get_check_constraints("jobs")]
        if "ck_jobs_trigger" not in triggers:
            with op.batch_alter_table("jobs") as b:
                b.drop_constraint("ck_jobs_trigger", type_="unique")
                b.create_check_constraint("ck_jobs_trigger", _in("trigger", JOB_TRIGGERS))
        elif "import" not in _read_trigger_values(bind):   # Werte prüfen, nicht nur den Namen
            with op.batch_alter_table("jobs") as b:
                b.drop_constraint("ck_jobs_trigger", type_="unique")
                b.create_check_constraint("ck_jobs_trigger", _in("trigger", JOB_TRIGGERS))
    ```
  - `downgrade()`: Spalte entfernen, Constraint auf die alte Werteliste zurücksetzen.
  - `batch_alter_table` deckt SQLite und MariaDB ab.
- [ ] **`tests/test_config.py`**: `IMPORT_MODE=windmill` → Settings-Validation-Error;
  `import_enabled` bei leerem Root + leerer URL = `False`.
- [ ] **Migrations-Test**: frische DB → `migrate()` ohne Fehler, Spalte `manual`
  vorhanden und `import` in `ck_jobs_trigger` erlaubt (Constraint-Werte lesen und prüfen).

> Abnahme Phase 1: `ruff check .` sauber, `pytest -q` grün, Alt-DB-Upgrade wie
> Frisch-DB-Upgrade ohne Fehler.

---

### Phase 2 — Feature 1: Manuelles Eintragen

- [ ] **`app/ytdlp.py`** – `playlist_id_from_url(url: str) -> str | None`
  - akzeptiert `https://www.youtube.com/playlist?list=PL…`, `&list=` in beliebigen
    YouTube-URLs und eine bare Playlist-ID; sonst `None`.
- [ ] **`app/discovery.py:62`** – einzeiliger Guard, sonst nichts an der Logik:
  ```python
  for pid, pl in existing.items():
      if pl.manual:            # manuelle Zeilen nie als "removed" markieren
          continue
      if pid not in seen and pl.remote_status != "removed":
          pl.remote_status = "removed"
          stats.removed += 1
  ```
  Titel-/Count-Refresh im oberen Teil bleibt aktiv (falls die Playlist doch im Kanal
  auftaucht). Damit bleibt `remote_status == "active"` und `sync_job` (`app/scheduler.py:120`)
  arbeitet manuelle Sync-Playlists normal ab.
- [ ] **`app/api.py`**
  - `playlist_dict()` → zusätzlich `"manual": pl.manual`
  - **neu `POST /api/playlists`**, Single Purpose, **kein Auto-Enqueue**
    (die neue Zeile hat `state="new"` und bekommt dadurch den bestehenden
    „Download now“-Button, `app/static/app.js:202`):
    1. Body `NewPlaylist(url: str, type: str = "oneshot", title: str | None = None)`;
       `type` nicht in `{sync, oneshot}` → **422**
    2. `pid = playlist_id_from_url(...)` → `None` → **422**
    3. `playlist_id` existiert schon → **409**
    4. `await ytdlp.list_playlist_entries(cfg.ytdlp_bin, pid, env)`;
       `YtDlpError` → **400** mit englischer Meldung (das ist die Server-seitige
       Validierung von Unlisted/Existenz/Privat)
    5. `Playlist(..., manual=True, remote_status="active", state="new",
       folder_name=sanitize_folder_name(titel, pid),
       remote_item_count=len(entries), first_seen_at=now, last_seen_at=now)`
    6. Rückgabe `playlist_dict(...)` (ohne `last_job`)
  - **neu `POST /playlists/{pid}/import`**
    - `not cfg.import_enabled` → **400** (`"import sources not configured"`)
    - sonst `_enqueue_or_409(request, pid, "import")`
    - done-Oneshots bleiben über `enqueue()` gesperrt (`trigger != "full_rerun"`)
      → 409, **gewollt** (Harte Regel 5), per Test festhalten
- [ ] **UI**
  - `app/static/index.html`: über `#sync-table` und `#oneshot-table` jeweils
    ```html
    <form class="add-form" data-type="sync">
      <input type="url" required placeholder="https://www.youtube.com/playlist?list=…">
      <button data-action="add-playlist" type="submit">Add playlist</button>
      <span class="muted add-msg" role="status"></span>
    </form>
    ```
    identisch im Oneshot-Tab mit `data-type="oneshot"`. **Nur englische Strings.**
  - `app/static/app.js`:
    - `submit`-Delegate pro Formular → `api("/playlists", { method:"POST",
      body:{ url, type: form.dataset.type } })`
    - Fehler (400/409/422) textlich in `.add-msg`, Eingabefeld nicht leeren,
      **kein `location.reload`** (davon gibt es einen Wächter-Test)
    - `playlist_dict`-Feld `manual` optional als Badge in den Titelzellen rendern
  - `app/static/style.css`: `.add-form` (Grid/Flex), im `@media (max-width: 768px)`
    Block als volle Breite unter der Tabellenkarte integrieren
- [ ] **`tests/test_api.py`**
  - `POST /playlists` legt manuelle Oneshot-Zeile an (`manual is True`,
    `remote_item_count`, `folder_name` set, `state == "new"`)
  - Duplikat → 409 · ungültige URL → 422 · falscher `type` → 422
  - Stub-Fehler (unbekannte Playlist-ID) → 400
  - **`test_manual_playlist_survives_discovery`**: manuelle Zeile anlegen,
    `apply_discovery([...ohne diese ID...])` → `remote_status` bleibt `"active"`,
    `stats.removed == 0`
  - `POST /{pid}/import` ohne konfigurierte Quellen → 400
  - `test_ui_is_english` weiter grün

> Abnahme Phase 2: URL eintippen → Zeile erscheint → „Download now“ läuft wie gehoben;
> Discovery macht die Zeile nicht zu `removed`.

---

### Phase 3 — Feature 2: Import (Trigger `import`)

- [ ] **`app/paths.py`** – `sanitize_filename(title: str, max_bytes: int = 150) -> str`
  (gleiche `_BAD_CHARS`, enthält bereits `/` und `\`; `sanitize_folder_name` ist für
  Dateien zu streng, weil es `[…]`-Suffix und Umlaubehandhabung mitbringt).
- [ ] **`app/ytdlp.py`**
  - `append_archive(path, ids: Iterable[str]) -> int` – **nur anhängen**, vorhandene
    IDs überspringen, niemals neu schreiben (nie löschen).
  - **Fix `evaluate()` (Zeile 261)**:
    ```python
    unavailable = {e.id for e in entries if e.unavailable} | (perm & set(ids))
    unavailable -= archive_ids     # archiviert gewinnt
    ```
    → `total == archived + unavailable + missing` gilt wieder. Wird bei Import und
    bei normalen Sync-Läufen sichtbar.
- [ ] **`app/importer.py` (neu, kein DB-Zugriff** – Layering-Regel aus AGENTS.md:
    DB-Logik bleibt in `jobqueue.py`/`api.py`)
  - `@dataclass ImportItem: id: str, index: int, title: str, src: Path | None`
  - `@dataclass ImportResult` (oder direkt `runner.RunResult` zurückgeben, s. u.)
  - `resolve(playlist_id, entries, archive_ids, dest_folder, cfg, log) -> list[ImportItem]`
    Auflösungsreihenfolge pro fehlendem Video:
    1. **Eigener Ordner**: existierende `NN - * [<id>].*` → Quelle = kein Kopieren,
       nur Archive-Zeile.
    2. **TubeArchivist-API** (wenn `cfg.ta_api_url`): `httpx.Client(timeout=…)` mit
       `Authorization: Token …`; `GET /api/video/<id>/` → `media_url` →
       `Path(cfg) / media_url.lstrip("/")`, `title` von dort. Membership-Fallback
       `GET /api/playlist/` + Abgleich über `playlist_url`. **Jeder Fehler →
       Abbruch dieser Stufe, still weiter mit Stufe 3**, Meldung ins Log.
    3. **FS-Scan** über `cfg.import_root_paths()`: Dateiname enthält
       `[<id>]` **oder** `-<id>.` **oder** `<id>.` **oder** endet auf `<id>` →
       erste Übereinstimmung (deckt TA- und TubeSync-Naming ab).
    4. Nichts gefunden → `src=None`, als `failed` zählen und loggen.
  - `execute(items, dest_dir, cfg, mode, cancel: threading.Event, dry_run,
    on_progress) -> runner.RunResult`
    - Zielname `f"{index:02d} - {sanitize_filename(title)} [{id}]{suffix}"`
    - Ziel existiert → nur Archive
    - `shutil.copy2` (Default) bzw. `os.link` (bei `OSError` verständliche Meldung
      „source and target must be on the same filesystem“, Job als `failed`)
    - Sidecars gleichen Stems mitkopieren
    - `cancel` zwischen den Dateien prüfen → `RunResult(cancelled=True)`
    - `dry_run` (Settings `dry_run`) → nichts schreiben, nur zählen/simulieren
    - Rückgabe als `RunResult(total=len(entries),
      archived=len(read_archive(...)) nach dem Lauf, new_count=frisch kopiert,
      skipped=bereits vorhanden, failed=unauflösbar)` → **`_finalize`
      (`app/jobqueue.py:304`) verarbeitet sie unverändert**, inkl. Oneshot → `done`
- [ ] **`app/jobqueue.py`**
  - `PRIORITY["import"] = 0`
  - `_run_job` (`app/jobqueue.py:217`): Zweig
    ```python
    if job.trigger == "import":
        # state="running", log_path, wie oben – aber kein run_playlist()
        self._import_cancel = threading.Event()
        try:
            result = await asyncio.to_thread(importer.execute, …)
        finally:
            self._import_cancel = None
        size = None if cfg.dry_run else await asyncio.to_thread(ytdlp.folder_size, …)
        self._finalize(job_id, playlist_type, result, size)
        return
    ```
    **Kein `ProcessHandle`** → genau ein Worker bleibt gewahrt.
  - `cancel()` (`app/jobqueue.py:99`) zusätzlich:
    `if job_id == self.current_job_id and self._handle is None and self._import_cancel: set()`
  - `self._import_cancel: threading.Event | None = None` in `__init__`
- [ ] **UI**
  - `app/api.py::status` → `"import_enabled": cfg.import_enabled`
  - `app/static/app.js`: `Import`-Button in `syncRow` (Zeile ~175) und `oneshotRow`
    (Zeile ~306), gerendert **nur wenn `state.status.import_enabled`**;
    Action `"import"` → `api(`/playlists/${id}/import`, { method:"POST" })`
  - Log-Button funktioniert unverändert weiter (der Import schreibt in dieselbe
    `logs/<jobid>.log`)
- [ ] **Stub `tests/fixtures/fake_ytdlp.py`**: bei unbekannter Playlist-ID
  `log_err("ERROR: …")` + `return 1` statt `KeyError`.
- [ ] **`tests/test_importer.py` (neu, rein, ohne Netzwerk)**
  - vorhandene Datei im Zielordner → nur Archive angehängt, nichts kopiert
  - FS-Scan findet TubeSync- (`…-id.mp4`) und TA-Style (`… [id].mp4`) Dateien,
    kopiert + benennt korrekt inkl. `.info.json`
  - `mode="hardlink"` → gleicher Inode; `mode="copy"` → eigene Inodes
  - `dry_run=True` → Zielordner/Archive unverändert
  - `cancel` gesetzt → abgebrochen, `cancelled=True`
  - keine Quelle gefunden → `failed == n`, `success` entsprechend
  - **Quelle bleibt unverändert** (mtime + Bytes vorher/nachher) – Harte Regel 1
  - TA-Backend gegen `httpx.MockTransport` (404 → still FS-Scan)
- [ ] **`tests/test_queue_discovery.py`**
  - Import-Job über `queue.start()` / `wait_for_jobs` (Muster Zeile 154):
    `job.status == "success"`, Oneshot danach `state == "done"`,
    Archive enthält die importierten IDs, `items_new == Kopien`
  - Abbruch-Test (`queue.cancel(job_id)` während `import`)
  - done-Oneshot + `POST /import` → 409
- [ ] **`tests/test_ytdlp.py`**: `test_evaluate_archived_unavailable_not_double_counted`

> Abnahme Phase 3: Button `Import` kopiert fehlende Videos inkl. Umbenennung, hängt
> Archive-Zeilen an, Oneshot springt auf `done`; ohne konfigurierte Quellen kein Button.

---

### Phase 4 — Doku + Rest

- [ ] **`SPEC.md`**
  - §5 ENV-Tabelle: `IMPORT_ROOTS`, `IMPORT_MODE`, `TA_API_URL`, `TA_API_TOKEN`
    (Default leer = Feature aus)
  - **§6.4a „Manuelle Playlists“**: Entstehung via `POST /playlists`, Discovery-Guard
    (`manual` wird nie `removed`), done-Oneshot-Sperre bleibt
  - **§6.9a „Import“**: Trigger `import`, Priorität 0, Auflösungsreihenfolge
    (Eigener Ordner → TA-API → FS-Scan), `copy`-Default, nie löschen, nur manuell
  - §7 Datenmodell: `playlists.manual`, `jobs.trigger`-Wert `import`
  - §8 API: `POST /playlists`, `POST /playlists/{pid}/import`, `status.import_enabled`
  - §9 Web UI: Add-Formular auf beiden Tabs, `Import`-Button
- [ ] **`README.md`** (Englisch): ENV-Einträge, Abschnitt „Importing from TubeSync /
  TubeArchivist“, Hinweis **`IMPORT_ROOTS` read-only mounten**, `TA_API_URL`/`TA_API_TOKEN`
- [ ] **`PLAN.md`**: neue Phase + Entscheidungslog (die 7 Punkte aus §2)
- [ ] Ggf. `docker-compose`-Beispiel-Volumes ergänzen (nur als Kommentar/Readme-Block,
  keine Secrets)
- [ ] Final: `.venv/bin/ruff check .` · `.venv/bin/pytest -q` ·
  `node --check app/static/app.js`

---

## 5. Reihenfolge der Ausführung

1. Phase 1 (Fundament) – ohne sie nichts weiter
2. Phase 2 (manuelles Eintragen) – eigenständig nutzbar, kleinstes Risiko
3. Phase 3 (Import) – größter Block, ggf. in 3a `importer.py` (rein, testbar) und
   3b `jobqueue`-Integration + UI aufteilen
4. Phase 4 (Doku)

Nach jeder Phase: `ruff check .` + `pytest -q`, `[x]` im Checklistenteil.

---

## 6. Risiken und offene Punkte

| # | Risiko | Gegenmaßnahme |
|---|---|---|
| 1 | **TA-API-Shape nur aus Doku/Beispielen**, keine Live-Instanz geprüft | FS-Scan ist die Garantie-Ebene; API ist Komfort. Jeder API-Fehler → still fallen, nie der Job. Bei Abweichung nur `app/importer.py` anpassen. |
| 2 | **`0002`-Migration auf Bestands-DB** (SQLite-Check-Constraint, `batch_alter_table`) | Existenz-Guard + Test auf Frisch-DB *und* auf DB im `0001`-Stand; Werte des Constraints lesen, nicht nur den Namen. |
| 3 | **Import trifft done-Oneshots nicht** (Harte Regel 5) | Absicht. In SPEC dokumentieren, Test `409`. Ausweg bleibt `full_rerun`. |
| 4 | **hardlink über Mount-Grenzen** | Default `copy`; bei `OSError` verständliche Fehlermeldung statt stillen Downturns. |
| 5 | **Sehr viele fehlende Videos** → langer Import-Block im Worker | `threading.Event`-Cancellation + Fortschritt ins Job-Log; Priorität 0 hält ihn vor Sync-Jobs. |
| 6 | **Datei-Namen-Kollisionen / Titel mit Sonderzeichen** | `sanitize_filename` + bestehende `_STEM`-Regex als Prüfung nach dem Kopieren; bei Schema-Bruch `failed` zählen. |
| 7 | **Quelle beschreiben/schreiben** | Nur `copy2`/`os.link`-Lesen; kein `unlink`/`rename` auf `IMPORT_ROOTS`. Test prüft Quell-Bytes vorher/nachher. |

---

## 7. Datei-Inventar (Was angefasst wird)

**Neu**
- `migrations/versions/0002_manual_and_import.py`
- `app/importer.py`
- `tests/test_importer.py`

**Geändert**
- `app/config.py`, `app/models.py`, `app/discovery.py`, `app/api.py`,
  `app/jobqueue.py`, `app/paths.py`, `app/ytdlp.py`
- `app/static/index.html`, `app/static/app.js`, `app/static/style.css`
- `tests/fixtures/fake_ytdlp.py`, `tests/test_api.py`,
  `tests/test_queue_discovery.py`, `tests/test_ytdlp.py`, `tests/test_config.py`
- `SPEC.md`, `PLAN.md`, `README.md`

**Nicht angefasst**
- `app/runner.py`, `app/scheduler.py`, `app/backup.py`, `app/healthchecks.py`,
  `app/main.py` (es sei denn `import_enabled` muss irgendwohin), `.github/`

---

## 8. Bekannter Ausgangsstand

- `.venv/bin/pytest -q` → **73 passed** (Stand nach `d2769ca`)
- `.venv/bin/ruff check .` → sauber
- `node --check app/static/app.js` → ok
- Letzte Commits: `f5b4228` (Responsive Layout), `d2769ca` (Oneshot-Default-Sort)
