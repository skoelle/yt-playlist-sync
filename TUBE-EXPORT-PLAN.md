# TUBE-EXPORT-PLAN: TubeSync → yt-playlist-sync

Operativer Ablauf für den ersten und einzigen echten Lauf von `python -m app.ts_export`
plus Wartungs-CLI `app.forget`. **Der Lauf ist am 2026-10-08 durchgeführt** – die
Sektion „Ergebnis" am Ende hält die tatsächlichen Werte samt Root Cause fest; die
Erwartungen in den Phasen sind der Rechenstand davor und waren zum Teil falsch.
Dieses Dokument ist eine einmalige Aufzeichnung (es gibt keinen zweiten Import),
die Sektion „Ergebnis" ist entsprechend historisch zu lesen. Code, Regeln und Tests:
`README.md` „Importing the exported tree" / „Forgetting a playlist",
`IMPORT-FEATURE.md` 3c, `SPEC.md` §14/§15.

> **Anonymisiert:** Pfade, Playlist-IDs und Playlist-Titel sind Platzhalter
> (`<data>`, `<tubesync>`, `<pid-n>`, `P1…P7`); Zählwerte und Zeitpunkte blieben,
> weil sie der eigentliche Inhalt der Aufzeichnung sind. Reihenfolge der
> Platzhalter entspricht der Reihenfolge im Befund unten.

## Entscheidungen

- **Alle 7 TubeSync-Playlists** kommen nach `<data>` (der App-Datenordner, `/data`).
- **Die 3 vorhandenen Ordner werden vorher vergessen** (`app.forget`, Default =
  Umzug nach `<data>/.quarantine`): `P5`, `P6`, `P7` – **App-Sync über yt-dlp**,
  ohne `manifest.json`.
- Vergeben wird nur mit `--apply`; `--delete-folder` kommt nicht zum Einsatz.

## Befund (read-only Recherche)

| Quelle | Videos in TubeSync | in `/data` | nur in `/data` | nur in TubeSync | Konsequenz |
|---|---|---|---|---|---|
| `P1` | 46 | – | – | – | neu |
| `P2` | 69 | – | – | – | neu |
| `P3` | 4 | – | – | – | neu |
| `P4` | 19 | – | – | – | neu |
| `P5` | 153 (1 ohne Datei) | 134 | **0** | 19 (18 mit Datei) | vorher `forget` (17 Doppeldateien sonst) |
| `P6` | 316 (5 ohne Datei) | 292 | **0** | 24 (19 mit Datei) | vorher `forget` (289 Doppeldateien sonst) |
| `P7` | 12 | 8 (alter Ordnername weicht ab) | **0** | 4 (alle mit Datei) | vorher `forget` (zweiter Ordner sonst) |

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
- Die App-DB liegt in `/config/app.db` (Host-Ordner des Deployments) und war von der
  Arbeitsplatte **nicht** erreichbar → `forget` und `ta_import` laufen im Container;
  der Export lief von der Arbeitsplatte (TubeSync-DB und `/data` sind sichtbar).
- `config/db.sqlite3-journal` vorhanden (0 B): bei `TsError` TubeSync stoppen, erneut.

## Pfade

```bash
TS=<tubesync>                       # TubeSync-Basisverzeichnis
DB=$TS/config/db.sqlite3            # nur lesen (file:…?mode=ro)
MEDIA=$TS/downloads                 # Videos: video/<jahr>/…mkv
THUMBS=$TS/config/media             # thumbs/<xx>/<uuid>.jpg
TARGET=<data>                       # App-Datenordner (/data)

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
`P1` (46) → `P2` (69) → `P5` (153) → `P3` (4) → `P6` (316) → `P7` (12) →
`P4` (19), bytes ≈ 21 G.
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
States: `P1` `done` 46/46, `P2` `done` 69/69, `P5` `idle` 152/153,
`P3` `done` 4/4, `P6` `idle` 311/316, `P7` `done` 12/12,
`P4` `done` 19/19 (`idle` = die 6 Videos ohne Datei).
Abbruch bei „open job, skipped" oder `updated` statt `new`.

**8 – Start und UI**
```bash
docker compose up -d
```
UI prüfen: 7 Playlists, Cover, Zähler, Galerie ohne Duplikate; danach alle 7 von
`oneshot` auf `sync` stellen – der nächste Nachtlauf lädt dann die 6 fehlenden
Videos nach und hält alles aktuell.

**9 – Später manuell:** `<data>/.quarantine/*` prüfen und löschen (nicht Teil der Tools).

**10 – Abschlussprüfung (Quarantäne gegen den Ordner):**
```bash
.venv/bin/python - <<'EOF'
from pathlib import Path
from app.ytdlp import _STEM
PID = "<pid-6>"
old = Path("<data>/.quarantine") / f"<alter-Ordnername> [{PID}]"
new = Path("<data>") / f"<ts-Ordnername> [{PID}]"
ids = lambda d: {m.group(3) for p in d.glob("*.mkv") if (m := _STEM.match(p.stem))}
fehlt = sorted(ids(old) - ids(new))
print(f"alt={len(ids(old))} neu={len(ids(new))} noch fehlend={len(fehlt)}: {fehlt}")
EOF
```
Erwartung: `noch fehlend=0`. **Ergebnis (2026-10-08):** `alt=292 neu=309`,
`noch fehlend=0` (Quarantäne vollständig enthalten) – die 7 ids, die im Ordner
fehlen, standen auch nie in der Quarantäne, siehe „Ergebnis Nachlauf". Restvideo-Grund wäre dann Verfügbarkeit auf YouTube
(privat/deleted) – in dem Fall die Datei aus der Quarantäne nachziehen und den
Ordner neu importieren (App stoppen, damit die Archiveinträge aus dem
`ta_import`-Merge stimmen; sonst lädt die App dieselbe ID mit anderem `NN`-Präfix
ein zweites Mal).

## Ergebnis (einmaliger Lauf, 2026-10-08)

Durchgeführt wie oben, Abweichungen gegenüber den Erwartungen:

| Schritt | Erwartet | Tatsächlich |
|---|---|---|
| Forget der 3 | `removed` oder `missing` | ausgeführt; Report-Wert nicht protokolliert. Nachweisbar: die alten Ordner liegen in `<data>/.quarantine` (`P6`: 292 mkv, Stand 2026-10-04), die Ordner in `/data` tragen die TubeSync-Titel |
| Export | `copied=613`, `miss=6`, `link=613` | **`copied=541`, `miss=78`**; **Kopien statt Hardlinks** (Inode-Stichprobe 0/64 gleiche Inode – getrennte Mounts, wie schon beim TA-Export `EXDEV`/`EPERM`), Archive `<target>/archives/<pid>.txt` = 541 Zeilen, 18:44–18:49 |
| Import | 7× `new`, `archive+=613` | 7 Playlists importiert; `P6` startete mit 251 Dateien (240 Export + 11 eigene App-Downloads bis zum Importzeitpunkt) |
| Nachlauf | 6 Videos fehlen | **65 fehlten** (App-Type `sync`, läuft), Zähler `downloaded_count` = Anzahl Archiveinträge (`ytdlp.py:305`, nach jedem Lauf in `jobqueue.py:338` neu gerechnet) → wächst automatisch mit |

**Root Cause der 78 `miss` (nicht der Export):** TubeSync selbst hatte die Dateien
nicht mehr auf der Platte – 619 Einträge, 613 mit `media_file`-Referenz, davon nur
**541 vorhanden**:

| Playlist | Einträge | ohne Referenz | Datei vorhanden | Referenz ins Leere |
|---|---|---|---|---|
| `P6` | 316 | 5 | 240 | **71** |
| `P3` | 4 | 0 | 3 | **1** |
| `P5` | 153 | 1 | 152 | 0 |
| `P1` / `P2` / `P7` / `P4` | 146 | 0 | 146 | 0 |
| Summe | 619 | 6 | 541 | 72 |

- Die alten App-Sync-Ordner in der Quarantäne enthielten **69 dieser 71** verlorenen
  Videos (plus 223 doppelt vorhandene) → zusammen mit dem Export sind **309 von 316**
  lokal abgedeckt, nur 7 IDs existieren nirgends mehr auf der Platte.
- Ein Sonderfall: der Export nutzt TubeSync-Titel, die App-Datenbasis für einen
  späteren Sync denselben Ordner – `.quarantine` behält den alten Namen mit
  Leerzeichen, der neue trägt die TubeSync-Schreibweise, also nie eine Kollision.
- **Hinfällig/kein zweiter Lauf:** Diese Zahlen sind reine Aufzeichnung. Es gibt
  keinen weiteren TubeSync-Import; für künftige Fälle zählt der reguläre Pfad
  (README „Importing the exported tree"), die Root-Cause-Tabelle ist nur der
  Beleg, warum der erste Lauf unter Erwartung lag.
- ~~Offen: Abschlussprüfung Phase 10 (Quarantäne gegen den Ordner)~~ – siehe
  „Ergebnis Nachlauf" unten: erledigt, Quarantäne vollständig enthalten.

## Ergebnis Nachlauf (2026-10-08, App-Run fertig)

- Ordner `P6` = **309/316**; die Quarantäne ist **vollständig enthalten**
  (alle 292 alten ids, `set(old) - set(new) = 0`), 17 ids kamen ausschließlich aus
  TubeSync-Dateien/Downloads.
- Die **7 fehlenden ids lagen nie in der Quarantäne** und existieren auf keinem
  Laufwerk: 5 ohne jede TubeSync-Referenz und 2 mit TubeSync-Referenz, deren Datei
  dort ebenfalls fehlt.
- Der App-Nachlauf hat sie offenbar nicht holen können (privat/gelöscht?);
  Nachweis in der UI an den Platzhalter-Zeilen (`unavailable`/`failed` mit Grund)
  bzw. im Job-Log. Falls YouTube sie doch liefert, reicht ein Retry/Full Re-Run –
  die ids stehen nicht im Archive, ein Lauf müsste sie also erneut versuchen.
- **Damit ist Phase 10 erledigt:** Quarantäne darf manuell bereinigt werden
  (Scheidung nur, wenn die 7 nicht wiederzubekommen sind – die 292 ids sind
  sowieso alle im Ordner, die Quarantäne enthält also nur Duplikate).

## Offene Punkte

- ~~Abschlussvergleich Quarantäne gegen Ordner (Phase 10)~~ – erledigt 2026-10-08,
  siehe „Ergebnis Nachlauf": Quarantäne vollständig enthalten, die 7 restlichen ids
  existieren nirgends. Offen ist nur noch, ob die 7 dauerhaft auf YouTube
  verschwunden sind (UI-Grund prüfen) und danach die Quarantäne manuell geleert
  wird.
- ~~Sind die 3 wirklich keine DB-Zeilen?~~ – Lauf gelaufen, Report-Wert nicht
  protokolliert; nachweisbar ist der Effekt (Ordner weg aus `/data`, neue mit
  TubeSync-Titel), siehe „Ergebnis".
- ~~Alter vs. TubeSync-Ordnername bei `P7`~~ – erledigt, in `/data` steht jetzt die
  TubeSync-Schreibweise, der alte Name liegt in der Quarantäne.
- NN-Konvention danach gemischt: TubeSync-Export nummeriert in Krawl-Reihenfolge,
  ein späterer App-Sync in YouTube-Position – nur kosmetisch (jede Datei trägt
  ihre Video-ID, keine Kollisionen); beim Nachlauf von `P6` sichtbar an den eigenen
  Dateien `88 - …` bis `100 - …`.

## Regeln

- Erst Report, dann Apply: `forget` (ohne `--apply` nur Anzeige), Export `--dry-run`,
  Import `--dry-run` – erst danach der echte Lauf.
- DB-Schritte (`forget`, `ta_import`) laufen im gestoppten Container, der Export von
  der Arbeitsplatte; die App bleibt das ganze Fenster gestoppt, damit die Discovery
  die 3 nicht neu anlegt und kein Job dazwischenkommt.
- Nichts löschen außer `forget` (Quarantäne statt Löschung); Rollback vor dem Import:
  Ordner nicht importieren, danach DB-Zeilen durch erneutes Vergessen entfernen.
