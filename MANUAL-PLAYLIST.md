# MANUAL-PLAYLIST: Manuelles Eintragen von Playlists

Feature-Idee / Entwurf – **noch nicht umgesetzt**.

Herkunft: das frühere `IMPORT-FEATURE.md` hatte zwei Teile. Teil 2 (fehlende Videos aus
TubeSync/TubeArchivist vervollständigen) ist fertig und steht nur noch in `SPEC.md`
(§14/§15) und `README.md` (Exporting / Importing / Forgetting) – aus diesem Dokument
entfernt. Übrig bleibt Teil 1: Playlists von Hand eintragen, die im Kanal-Listing des
Kanals nicht auftauchen (unlisted, gelöschte, fremde).

Quelle der Wahrheit für Verhalten ist `SPEC.md`, für die Bedienung `README.md`. Dieses
Dokument speichert nur den Entwurf, bis das Feature gebaut ist; danach Verhalten nach
SPEC/README überführen und diese Datei zusammenschneiden (AGENTS.md „Dokumentation und
Stand").

---

## 1. Ziel

**Manuelles Eintragen beliebiger Playlists** (inkl. *unlisted*, kein YouTube-Login, kein
API-Key): URL in die UI eintippen → Zeile in der DB → einmal eingeben, nie automatisch
wieder gelöscht.

Nicht Teil des Features: automatischer Import, Ergänzen aus externen Archiven (erledigt),
Löschen irgendetwas, YouTube-Login.

---

## 2. Bestätigte Entscheidungen

| # | Frage | Entscheidung |
|---|---|---|
| 1 | Ausführung | **Nur manuell (Formular je Playlist-Typ)** – kein Cron, kein Auto-Mitlauf in der Nightly-Sync |
| 2 | Formular | **Beide Tabs** (Sync + Oneshot), Typ folgt dem aktiven Tab |
| 3 | Playlists selbst | **Unlisted reicht**, kein Login nötig – validiert wird per `yt-dlp --flat-playlist -J` |
| 4 | Titel beim Anlegen | **Titel = Playlist-ID**, kein eigener Titelabruf (Nebenfolgen siehe 3.4) |

Die übrigen Fragen des ursprünglichen Fragebogens (Import-Modus `copy`/`hardlink`,
Zugriff auf Quelldaten, Queue-Trigger `import`, Membership über die TA-API) gehörten zum
erledigten Teil und sind entfernt.

Projektregeln, die gelten (AGENTS.md / SPEC):

- **Harte Regel 1 – nie löschen.** Das Anlegen fügt nur eine Zeile hinzu.
- **Harte Regel 4 – kein Login/Secrets/API-Key** für YouTube. Die Validierung läuft ohne
  Anmeldung über yt-dlp.
- **Harte Regel 5 – Oneshot `done` bleibt `done`.** Bestehende Zeilen werden beim Anlegen
  nie angefasst.
- **Harte Regel 9 – Tests ohne Netzwerk.** Validierung über den Stub
  `tests/fixtures/fake_ytdlp.py`.
- **Sprachregel:** Code/Kommentare/Tests/README Englisch, `SPEC.md`/`PLAN.md`/dieses
  Dokument Deutsch, **UI-Texte Englisch** (Wächter `tests/test_api.py::test_ui_is_english`).

---

## 3. Offene Umsetzung

**Basis im Code (vorhanden):**

- `ytdlp.list_playlist_entries(ytdlp_bin, pid, env)` (`app/ytdlp.py:179`) →
  `--flat-playlist -J`, liefert `VideoEntry` (id, Titel, unavailable, position, duration)
  – Validierung und Inhalte kommen fertig; ein Fehler bedeutet ungültig/privat/gelöscht.
- `ytdlp.playlist_url(pid)`, `paths.sanitize_folder_name(title, pid)`, `paths.is_oneshot()`.
- Discovery aktualisiert Titel und Counts für bekannte, aktive Zeilen
  (`app/discovery.py:54`) und markiert nichts mehr als `removed` (Gone-Regel) → **kein**
  Guard gegen das Entfernen manueller Zeilen nötig; der frühere Planungspunkt dazu entfällt.
- `folder_name` wird erst beim ersten Job gesetzt (`app/jobqueue.py:228`) – das Formular
  muss die Zeile also nicht komplett fertig machen.
- Der Stub liest `data["playlists"][pid]`; eine unbekannte ID wirft aktuell `KeyError`
  (siehe 4).

**Offene Punkte:**

1. **`app/ytdlp.py`:** `playlist_id_from_url(url) -> str | None` – akzeptiert
   `…/playlist?list=<id>`, `&list=` in beliebigen YouTube-URLs und eine bare Playlist-ID,
   sonst `None`.
2. **`app/models.py` + Alembic:** Spalte `playlists.manual` (Boolean, Default `false`,
   keine Änderung an den CheckConstraints) sowie das Feld `manual` in `playlist_dict()`.
3. **`POST /api/playlists`** (`app/api.py`): Body `NewPlaylist(url, type="oneshot",
   title=None)`. Ablauf: unbekannter `type` oder `playlist_id_from_url` liefert `None` →
   422 · `list_playlist_entries()` schlägt fehl → 400 mit yt-dlp-Meldung (privat,
   gelöscht, unbekannt) · `playlist_id` existiert bereits → 409 · sonst Zeile
   `Playlist(..., manual=True, title=playlist_id, remote_status="active", state="new",
   remote_item_count=len(entries), first_seen_at=now, last_seen_at=now)`. **Kein
   Auto-Enqueue** – die neue Zeile bekommt damit den bestehenden „Download now"-Button.
4. **Titel (Entscheidung 2.4):** `title = playlist_id`. Nebenfolge für den Ordner: genau
   dieser Anwendungsfall (Playlists ohne Kanal-Listing) sorgt dafür, dass der Titel nie
   durch einen echten ersetzt wird – der erste Lauf legt deshalb `<id> [<id>]` an
   (`sanitize_folder_name(pl.title, pl.playlist_id)`, `app/jobqueue.py:229`). Rein
   kosmetisch, jede Datei trägt ihre Video-ID. Falls später gewünscht: Playlist-Titel aus
   dem Root-Feld derselben `-J`-Antwort holen (`parse_video_listing` bekommt das
   vollständige JSON, es wird nur verworfen). Bewusst **nicht** Teil dieser Entscheidung;
   `list_playlist_entries` wird nicht umgebaut, weil `app/runner.py:96` sie für die
   Gone-Erkennung nutzt.
5. **UI** (`app/static/`): Add-Formular über beiden Tabellen (URL-Eingabe, Button,
   Statuszeile), nur englische Strings, Fehler als Text ins Formular **ohne
   `location.reload`** (Wächter-Test), `manual` optional als Badge in der Titelspalte,
   unter 768 px als Block über der Zeile/Karte.
6. **Tests:** Zeile wird angelegt (Felder geprüft), 422/409/400, Stub-Fehlerfall,
   `test_ui_is_english` bleibt grün – alles ohne Netzwerk.
7. **Doku nach dem Bau:** `SPEC.md` §6 (Anforderung), §7 (`manual`), §8 (Endpunkt),
   §9 (UI), §14 (Changelog) und `README.md`.

---

## 4. Risiken

| Risiko | Gegenmaßnahme |
|---|---|
| Migration auf Bestands-DB (neue Spalte) | Alembic mit Existenz-Guard: Spalte prüfen, bevor sie angelegt wird; Test auf Frisch- **und** Alt-DB – Werte lesen, nicht nur den Namen |
| Fertige Oneshots | Kein Auto-Enqueue beim Anlegen → kein sofortiger 409; Harte Regel 5 bleibt über `enqueue()` gewahrt |
| Stub wirft `KeyError` bei unbekannter Playlist-ID | Fehlerfall im Stub sauber machen (exit 1 + Meldung) oder eigene `listing_error`-Map wie bei den Gone-Tests |
| Doppelte Eingabe desselben Links | 409 auf die existierende `playlist_id`, keine zweite Zeile |
| Ordner `<id> [<id>]` (siehe 3.4) | Akzeptiert oder später über den Playlist-Titel aus dem Root-Feld der `-J`-Antwort ersetzen |
