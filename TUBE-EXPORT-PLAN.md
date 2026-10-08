# TUBE-EXPORT-PLAN: TubeSync → yt-playlist-sync

Operativer Ablauf für den ersten echten Lauf von `python -m app.ts_export` plus
Wartungs-CLI `app.forget` (Stand 2026-10-08). Code, Regeln und Tests: `README.md`
„Importing the exported tree" / „Forgetting a playlist", `IMPORT-FEATURE.md` 3c,
`SPEC.md` §14/§15.

## Entscheidungen

- **Alle 7 TubeSync-Playlists** kommen nach `<data>`.
- **Die 3 vorhandenen Ordner werden vorher vergessen** (`app.forget`, Default =
  Umzug nach `<data>/.quarantine`): <P5>, <P6>, <P7> –
  **App-Sync über yt-dlp**, ohne `manifest.json`.
- Vergeben wird nur mit `--apply`; `--delete-folder` kommt nicht zum Einsatz.

## Befund (read-only Recherche)

| Quelle | Videos in TubeSync | in `/data` | nur in `/data` | nur in TubeSync | Konsequenz |
|---|---|---|---|---|---|
| 2023 | 46 | – | – | – | neu |
| 2024 | 69 | – | – | – | neu |
| 2025 | 4 | – | – | – | neu |
| <P4> | 19 | – | – | – | neu |
| <P5> | 153 (1 ohne Datei) | 134 | **0** | 19 (18 mit Datei) | vorher `forget` (17 Doppeldateien sonst) |
| <P6> | 316 (5 ohne Datei) | 292 | **0** | 24 (19 mit Datei) | vorher `forget` (289 Doppeldateien sonst) |
| <P7> | 12 | 8 als `<P7>` | **0** | 4 (alle mit Datei) | vorher `forget` (zweiter Ordner sonst) |

- **TubeSync ist Obermenge**: `nur_in_/data = 0` überall – in `/data` liegt nichts,
  was TubeSync nicht auch hat. Die 41 fehlenden Videos (18+19+4, alle mit Datei)
  kommen mit dem Export der 7 nach `/data`.
- Die 6 Einträge ohne `media_file` sind in TubeSync **und** in `/data` nicht vorhanden
  (identische Lücke, keine doppelte Lücke).
- Die Ordner sind App-Sync: yt-dlp schreibt `.info.json`/`.description`/JPG-Thumbnail
  (`app/ytdlp.py:216-218`); die `00 - …`-Triade ist yt-dlp-Playlist-Metafile
  (Position 0), kein TA-Cover.
- Gesamt: 7 Quellen, 619 Einträge, 613 mit Datei, 6 `miss`, alle `source_type='p'`.
- Hardlinks möglich: Quelle und Ziel haben `st_dev = 55`, 9,4 T frei.
- Die App-DB liegt in `/config/app.db` (Host `/opt/docker-local/yt-playlist-sync`) und ist
  von der Arbeitsplatte **nicht** erreichbar → `forget` und `ta_import` laufen im Container;
  der Export läuft von hier (TubeSync-DB und `/data` sind sichtbar).
- `config/db.sqlite3-journal` vorhanden (0 B): bei `TsError` TubeSync stoppen, erneut.

## Pfade

```bash
TS=<tubesync>
DB=$TS/config/db.sqlite3          # nur lesen (file:…?mode=ro)
MEDIA=$TS/downloads               # Videos: video/<jahr>/…mkv
THUMBS=$TS/config/media           # thumbs/<xx>/<uuid>.jpg
TARGET=<data>   # App-Datenordner (/data)

FORGET="--playlist <pid-5> --playlist <pid-6> --playlist <pid-7>"
```

## Phasen (ein gestopptes Fenster)

**0 – Stand holen** (ändert nur das Repo)
```bash
git checkout main && git pull
.venv/bin/pytest -q && .venv/bin/ruff check .
```

**1 – Image holen** (auf dem NAS, App läuft noch)
```bash
docker compose pull
```

**2 – App stoppen**
```bash
docker compose stop yt-playlist-sync
```

**3 – Forget der 3 (im Container, erst Report)**
```bash
docker compose run --rm yt-playlist-sync python -m app.forget $FORGET           # Report
docker compose run --rm yt-playlist-sync python -m app.forget $FORGET --apply    # ausführen
```
Erwartung: 3 Zeilen, `blocked=0`; DB-Zeilen `removed` **oder** `missing` (falls die
Playlists gar nicht in der DB stehen – Ordner/Archive werden trotzdem geräumt),
`folders=3`, `quarantine=<data>/.quarantine`, 2 Backup-Dateien unter `/backup`.
Abbruch bei `blocked` (App läuft/Job offen) oder `error` (Ordnerkonflikt).

**4 – Dry-Run des Exports (von hier, schreibt gar nichts)**
```bash
.venv/bin/python -m app.ts_export --dry-run --metadata-only \
  --db $DB --media-root $MEDIA --thumbs-root $THUMBS --target $TARGET
```
Erwartung: `playlists=7 videos=619 copied=613 miss=6 failed=0`, Reihenfolge
2023 (46) → 2024 (69) → <P5> (153) → 2025 (4) → <P6> (316) →
<P7> (12) → <P4> (19), bytes ≈ 21 G.
Abbruch bei `failed>0` oder fehlender/zusätzlicher Playlist.

**5 – Metadata-only (echter Lauf)**
Gleicher Befehl ohne `--dry-run` → 7 Ordner + 613 `.info.json` + 613 `.jpg`
+ 7 `manifest.json` + 7 `archives/<pid>.txt`, keine `.mkv`.
Stichprobe: `ls`, JSON-Felder (`title`, `duration`, `upload_date`), Einträge im Manifest.

**6 – Full Export**
Gleicher Befehl ohne `--dry-run` und ohne `--metadata-only` (`auto` → Hardlink)
→ `link=613 miss=6 failed=0`, kein Zusatzplatz. Inode-Stichprobe: `stat -c %i`.
Alternative für Unabhängigkeit von TubeSync: `--mode copy` (≈21 G extra).

**7 – Import (im Container)**
```bash
docker compose run --rm yt-playlist-sync python -m app.ta_import --dry-run   # Report
docker compose run --rm yt-playlist-sync python -m app.ta_import             # ausführen
```
Erwartung: 7× `action=new`, `archive+=613`,
States: 2023 `done` 46/46, 2024 `done` 69/69, <P5> `idle` 152/153,
2025 `done` 4/4, <P6> `idle` 311/316, <P7> `done` 12/12,
<P4> `done` 19/19 (`idle` = die 6 Videos ohne Datei).
Abbruch bei „open job, skipped" oder `updated` statt `new`.

**8 – Start und UI**
```bash
docker compose up -d
```
UI prüfen: 7 Playlists, Cover, Zähler, Galerie ohne Duplikate; danach alle 7 von
`oneshot` auf `sync` stellen – der nächste Nachtlauf lädt dann die 6 fehlenden
Videos nach und hält alles aktuell.

**9 – Später manuell:** `/data/.quarantine/*` prüfen und löschen (nicht Teil der Tools).

## Offene Punkte

- Sind die 3 wirklich keine DB-Zeilen? Der Forget-Report beantwortet das
  (`removed` vs. `missing`); die App-DB ist von hier nicht lesbar.
- `<P7>` (Ordner) vs. `<P7>` (TubeSync-Titel):
  der alte Name verschwindet mit der Quarantäne, der neue Ordner bekommt die
  TubeSync-Schreibweise.
- NN-Konvention danach gemischt: TubeSync-Export nummeriert in Krawl-Reihenfolge,
  ein späterer App-Sync in YouTube-Position – nur kosmetisch (jede Datei trägt
  ihre Video-ID, keine Kollisionen).

## Regeln

- Erst Report, dann Apply: `forget` (ohne `--apply` nur Anzeige), Export `--dry-run`,
  Import `--dry-run` – erst danach der echte Lauf.
- DB-Schritte (`forget`, `ta_import`) laufen im gestoppten Container, der Export von
  hier; die App bleibt das ganze Fenster gestoppt, damit die Discovery die 3 nicht
  neu anlegt und kein Job dazwischenkommt.
- Nichts löschen außer `forget` (Quarantäne statt Löschung); Rollback vor dem Import:
  Ordner nicht importieren, danach DB-Zeilen durch erneutes Vergessen entfernen.
