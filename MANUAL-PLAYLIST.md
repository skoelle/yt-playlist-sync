# MANUAL-PLAYLIST: Manuelles Eintragen von Playlists

**Fertig umgesetzt** – dieses Dokument war der Entwurf und dient jetzt nur noch als Verweis.

Das Verhalten steht in `SPEC.md` (§6.10 Anforderung, §7 Spalte `manual`, §8
`POST /api/playlists`, §9 Formular, §14 Changelog), die Bedienung in `README.md`
(„Adding a playlist manually").

Zwei Entscheidungen aus dem Entwurf, die nicht als eigener Fließtext in die SPEC
gewandert sind:

- **Nightly-Sync:** manuelle Sync-Playlists laufen im nächtlichen Sync mit – der
  bestehende Filter (`type=sync`, nicht `ignored`, `remote_status=active`) hängt
  nicht an der Herkunft der Zeile.
- **Titel:** optionales Titel-Feld im Formular; leer = Playlist-ID (Entscheidung 2.4),
  ausgefüllt = Ordnername `<Titel> [<id>]`.

Herkunft: Teil 1 des früheren `IMPORT-FEATURE.md` (Playlists von Hand eintragen, die
im Kanal-Listing nicht auftauchen – unlisted, gelöscht, fremde). Teil 2 (Import aus
TubeSync/TubeArchivist-Exporten) steht in SPEC §16/§17 und im README.
