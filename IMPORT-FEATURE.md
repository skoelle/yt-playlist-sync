# IMPORT-FEATURE: Manuelles Eintragen + Import aus TubeSync/TubeArchivist

Designdokument für das Feature „manuelle Playlists eintragen" und „fehlende Videos aus
externen Archiven vervollständigen". Kein Quelltext.

**Alle Phasen sind abgeschlossen.** Von der ursprünglichen Planung bleiben nur die drei
Dinge, die für spätere Entwicklung noch Wert haben: die Ziele (1), die bestätigten
Entscheidungen (2) und die Risiken mit ihren Gegenmaßnahmen (6). Die früheren Abschnitte
3–5 (Recherche-Stand, Phasenchecklisten, Reihenfolge) sowie 7 (Datei-Inventar) und
8 (Ausgangsstand) sind umgesetzt und wurden entfernt – Standwerte dort wären falsch.

**Quelle der Wahrheit für Verhalten ist `SPEC.md`** (§6, §14, §15) und für die Bedienung
`README.md` (Exporting / Importing / Forgetting). Eine Verhaltensänderung gehört dorthin;
hier nur nachziehen, wenn eine der Entscheidungen unten revidiert wurde.

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

**Revidiert beim Bauen** (bleibt als Lektion, gilt aber nicht mehr):

- **Zeile 4:** TubeSync ist **nicht** FS-only – `app/ts_export.py` liest die TubeSync-
  SQLite strikt read-only, das ist zuverlässiger als jeder Dateinamen-Scan.
- **Zeile 6:** Es gibt **keinen** Queue-Trigger `import` und kein `IMPORT_ROOTS`/`IMPORT_MODE`
  – die Übernahme läuft als Offline-CLI (`python -m app.ta_import`) gegen die gestoppte
  App, ebenso die Exporte (`ta_export`, `ts_export`) und das Aufräumen (`forget`). Damit
  entfällt auch die geplante Migration mit `manual`-Spalte und erweitertem Trigger-Constraint.
- **Zeile 3:** kein ENV-Modusschalter; die Exporte haben ein `--mode auto|copy|hardlink`
  (Default `auto` = Hardlink-Versuch mit Copy-Fallback, weil getrennte Mounts `EXDEV`/
  `EPERM` liefern), `ta_import` kopiert.

Projektregeln, die gelten (AGENTS.md / SPEC):

- **Harte Regel 1 – nie löschen.** Import/Empfang kopiert nur und hängt Archive-Zeilen an.
  Die Quelle ist nur lesend. Ausnahmen bleiben Logs/Backups-Rotation.
- **Harte Regel 2 – ein Worker.** Ein Lauf der App bewegt nie zwei Dinge gleichzeitig;
  die Offline-CLIs laufen ohnehin nur gegen die gestoppte App, kein zweiter paralleler Prozess.
- **Harte Regel 4 – kein Login/Secrets/API-Key** für YouTube. `TA_API_TOKEN` ist ein
  Deployment-Detail wie `DATABASE_URL` (ENV, nicht im Repo).
- **Harte Regel 5 – Oneshot `done` bleibt `done`.** Import darf done-Oneshots nicht
  anfassen; nur `full_rerun` öffnet sie wieder. Wird in SPEC + Test festgehalten.
- **Harte Regel 9 – Tests ohne Netzwerk.** TA-API wird über `httpx.MockTransport`
  gefaked, FS-Quellen über `tmp_path`-Bäume, yt-dlp über den Stub.
- **Sprachregel:** Code/Kommentare/Tests/README Englisch, `SPEC.md`/`PLAN.md` Deutsch,
  **UI-Texte Englisch** (Wächter `tests/test_api.py::test_ui_is_english`).

---

## 6. Risiken und offene Punkte

Die Tabelle stammt aus der Planungsphase; Gegenmaßnahmen zu den nicht gebauten Teilen
(Queue-Trigger, `IMPORT_ROOTS`) lesen sich als Lektion für spätere Vorhaben, nicht als
Zustand des Codes.

| # | Risiko | Gegenmaßnahme |
|---|---|---|
| 1 | **TA-API-Shape nur aus Doku/Beispielen**, keine Live-Instanz geprüft | FS-Scan ist die Garantie-Ebene, API ist Komfort; live geprüft. Jeder API-Fehler → still fallen, nie der Job. Bei Abweichung nur `app/ta_export.py` bzw. `app/export_common.py` anpassen. |
| 2 | **Migration auf Bestands-DB** (SQLite-Check-Constraint, `batch_alter_table`) | Existenz-Guard + Test auf Frisch-DB *und* auf Alt-DB; Werte des Constraints lesen, nicht nur den Namen. |
| 3 | **Import trifft done-Oneshots nicht** (Harte Regel 5) | Absicht. In SPEC dokumentiert, Test `409`. Ausweg bleibt `full_rerun`. |
| 4 | **hardlink über Mount-Grenzen** | Default `copy` bzw. `auto` mit Fallback; bei `OSError` verständliche Fehlermeldung statt stillen Downturns. |
| 5 | **Sehr viele fehlende Videos** → langer Import-Block | Offene Cancellation (bei der Offline-CLI: Prozessabbruch) + Fortschritt ins Log; ein Import bewegt keinen Download-Worker mit. |
| 6 | **Datei-Namen-Kollisionen / Titel mit Sonderzeichen** | `sanitize_filename` + bestehende `_STEM`-Regex als Prüfung nach dem Kopieren; bei Schema-Bruch `failed` zählen. |
| 7 | **Quelle beschreiben/schreiben** | Nur `copy2`/`os.link`-Lesen; kein `unlink`/`rename` auf den Quellpfaden. Test prüft Quell-Bytes vorher/nachher. |
