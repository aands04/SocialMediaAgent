# Verifizierte Vereins- und Gegnerlogos

## Grundsatz

Vereinswappen sind keine generativen Inhalte. OpenAI erhält nur das freigegebene
Spielerfoto als Bildreferenz und wird im versionierten Prompt angewiesen, keine
Wappen, Logos, Embleme, Marken oder Platzhalter zu erzeugen. Die Anwendung
speichert die unveränderte KI-Grundgrafik getrennt von der finalen PNG-Datei.
Erst danach setzt `verified-logo-compositor-v1` die geprüften Originaldateien
lokal und reproduzierbar ein.

Die Anwendung kann nicht zuverlässig erkennen, ob das Modell außerhalb der
geschützten Logobereiche dennoch ein fiktives Wappen erzeugt hat. Jede
KI-Grafik bleibt deshalb manuell freigabepflichtig.

## Upload und Versionierung

- Erlaubt sind PNG und WebP bis 5 MiB und 32–4096 Pixel je Achse.
- Endung, MIME-Type, tatsächliches Bildformat, technische Lesbarkeit,
  Abmessungen und SHA-256 werden serverseitig geprüft.
- Der Originaldateiname wird nur als Metadatum gespeichert. Der interne Pfad
  wird zufällig erzeugt und auf `UPLOAD_ROOT/logos/teams` beziehungsweise
  `UPLOAD_ROOT/logos/opponents` beschränkt.
- Das Original bleibt bytegenau erhalten. Eine optionale PNG-Ableitung darf nur
  proportional skaliert und auf transparente Randflächen gesetzt werden.
- SVG, externe URLs, Downloads aus dem Internet, Symlink-Ausbrüche und
  Pfad-Traversal werden nicht akzeptiert.
- Doppelte Prüfsummen werden wiederverwendet; neue inhaltliche Dateien erhalten
  eine neue, unveränderliche Version.

## Dashboard

Administratoren verwalten das eigene Logo unter **Mannschaften**. Bei jedem
Spiel führt **Gegnerlogo hochladen / auswählen** zu einer geschützten
Verwaltungsseite. Exakte Treffer des normalisierten Gegnernamens werden nur
vorgeschlagen und erst nach Bestätigung zugeordnet. Mannschaftszusätze wie
`II`, `Frauen`, `U19` oder Bestandteile einer Spielgemeinschaft bleiben Teil
des normalisierten Namens.

Ohne Gegnerlogo wird der ausgeschriebene Gegnername in einer neutralen
Formfläche gesetzt. Ohne eigenes Logo wird ein OpenAI-Generierungsauftrag vor
dem ersten kostenpflichtigen Bildaufruf auf `manual_review_required` gesetzt.

## Hintergrundjobs und Wiederaufnahme

Beim Einreihen werden Logo-ID, Version, SHA-256 und Pfad in den Jobparametern
eingefroren. Vor dem Modellaufruf prüft der Worker Datenbankstatus, Originalpfad
und Prüfsumme. Danach gelten die Phasen `loading_verified_logos`,
`generating_ai_base`, `compositing_logos` und `validating_final_media`.

Eine vollständig gespeicherte Grundgrafik wird bei sicherer Wiederaufnahme
wiederverwendet. Tritt erst bei der Logo-Komposition ein Fehler auf, bleibt sie
erhalten und der Auftrag wechselt zur manuellen Prüfung. Der Button
**Logos lokal neu zusammensetzen** verwendet diese Grundgrafik und verursacht
keinen weiteren OpenAI-Bildaufruf.

Logoänderungen erhöhen die Beitragsversion, entziehen offenen Aufträgen die
Freigabe und überschreiben keine vorhandenen Dateien. Veröffentlichte Aufträge
bleiben unverändert. Legacy-Beiträge ohne Logo-Snapshot werden gekennzeichnet
und nicht automatisch auf möglicherweise falsche Logos migriert.

## Proxmox-Staging

Nach dem Update:

```bash
git switch main
git pull --ff-only
docker compose --env-file .env.staging \
  -f docker-compose.yml -f docker-compose.staging.yml up -d --build
sudo bash -lc '
  cd /opt/socialmediaagent
  set -a
  . ./.env.staging
  set +a
  ./scripts/staging-check.sh
'
```

Das bestehende Hostverzeichnis `${STAGING_DATA_ROOT}/uploads` wird weiterhin
nach `/app/data/uploads` eingebunden. `UPLOAD_ROOT=/app/data/uploads` kann in
`.env.staging` ausdrücklich gesetzt werden. Zuerst das eigene Mannschaftslogo,
danach je Spiel ein Gegnerlogo oder den neutralen Fallback prüfen. Instagram
bleibt im Staging unverändert im Dry-Run.
