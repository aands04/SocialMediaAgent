# Verifizierte Vereins- und Gegnerlogos

## Grundsatz

OpenAI erhält die geprüften lokalen Referenzbilder in fester Reihenfolge:

1. Spielerfoto als Identitätsreferenz,
2. verifiziertes Originalwappen der eigenen Mannschaft,
3. optional das verifizierte Originalwappen des Gegners.

Der versionierte Sicherheitspräfix aus dem Universalprompt weist das Modell an,
den Spieler als dominantes Motiv zu verwenden, das eigene Logo deutlich und
harmonisch sowie das Gegnerlogo kleiner, aber klar erkennbar in die
Gesamtkomposition einzubinden. Form, Farben, Schriftzüge und
Emblembestandteile sollen den Referenzen entsprechen. Ohne Gegnerlogo wird
ausschließlich eine neutrale typografische Darstellung des Gegnernamens
verlangt; ein Wappen-Fallback ist verboten.

Ein Bildmodell kann die pixelgenaue Wiedergabe eines Logos oder das vollständige
Fehlen zusätzlicher Fantasieelemente nicht garantieren. Die Anwendung zeigt
deshalb die eingefrorenen Originale neben der Grafik an und verlangt weiterhin
eine manuelle Freigabe.

## Upload und Versionierung

- Erlaubt sind PNG und WebP bis 10 MiB, 32–8192 Pixel je Achse und 40 Millionen Bildpunkte insgesamt.
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

Ohne Gegnerlogo verlangt der Bildprompt den ausgeschriebenen Gegnernamen in
einer neutralen typografischen Lösung. Ohne eigenes Logo wird ein
OpenAI-Generierungsauftrag vor
dem ersten kostenpflichtigen Bildaufruf auf `manual_review_required` gesetzt.

## Hintergrundjobs und Wiederaufnahme

Beim Einreihen werden Logo-ID, Version, SHA-256 und Pfad in den Jobparametern
eingefroren. Vor dem Modellaufruf prüft der Worker Datenbankstatus,
Originalpfad und Prüfsumme. Danach gelten für neue KI-Grafiken die Phasen
`loading_verified_logos`, `generating_ai_composition` und
`validating_final_media`.

Nach erfolgreicher Speicherung wird die finale KI-Komposition bei einer
sicheren Wiederaufnahme wiederverwendet und nicht erneut kostenpflichtig
erzeugt. Für neue KI-Ausgaben existiert keine separate Logoebene: Eine
Logoänderung erfordert **Grafiken neu erzeugen** und damit einen neuen
OpenAI-Bildauftrag. Die Referenzreihenfolge und die verwendete
`verified-logo-ai-references-v1`-Policy werden im Medien-Snapshot gespeichert.

Logoänderungen erhöhen die Beitragsversion, entziehen offenen Aufträgen die
Freigabe und überschreiben keine vorhandenen Dateien. Veröffentlichte Aufträge
bleiben unverändert. Der lokale `verified-logo-compositor-v1` bleibt nur für
Legacy-Beiträge mit separat gespeicherter KI-Grundgrafik verfügbar.
Legacy-Beiträge ohne Logo-Snapshot werden gekennzeichnet und nicht automatisch
auf möglicherweise falsche Logos migriert.

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
danach je Spiel ein Gegnerlogo oder die neutrale Typografie prüfen. Bei jeder
KI-Ausgabe sind beide eingebetteten Logos visuell mit den im Dashboard
angezeigten Originalen zu vergleichen. Instagram bleibt im Staging unverändert
im Dry-Run.
