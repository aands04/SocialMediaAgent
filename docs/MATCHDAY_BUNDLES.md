# Gemeinsame Spieltagsbeiträge und Gegnerlogo-Katalog

## Gemeinsame Generierung

Wenn unter **Regeln & Storys** die Feed-Bündelung für Ankündigungen oder
Ankündigungen und Ergebnisse aktiv ist, fasst **Spiele & Testdaten** Spiele
aktiver Mannschaften desselben Vereins, derselben Instagram-Seite und desselben
Berliner Kalendertags zusammen. Die bevorzugte Mannschaft bestimmt weiterhin
die Reihenfolge der Feed-Bilder.

Ein Klick auf **Gemeinsame Ankündigung erzeugen** beziehungsweise
**Gemeinsames Ergebnis erzeugen** legt genau einen persistenten
Koordinatorauftrag an. Der Auftrag friert alle Spiel-IDs und Logoversionen ein,
erzeugt für jedes Spiel ein eigenes Feed-Bild und eigene Story-Medien und ruft
die Textgenerierung genau einmal mit den Fakten aller Spiele auf. Anschließend
wird genau ein Feed-Karussell erzeugt. Story-Aufträge bleiben pro Spiel
getrennt.

Für gemeinsame Ergebnisse müssen alle Ergebnisse bestätigt sein. Ändert oder
fehlt ein eingefrorenes Logo, stoppt der Auftrag vor dem nächsten
kostenpflichtigen Schritt. Teilweise bereits vorhandene Beiträge werden nicht
automatisch überschrieben.

Administratoren und berechtigte Redakteure können Spiele desselben Vereins,
desselben Instagram-Ziels und desselben Spieltags ausdrücklich verbinden. Mit
**Spiele bewusst trennen** werden sie dauerhaft aus der automatischen Gruppe
genommen, bis sie erneut bewusst verbunden werden.

## Systemweiter Gegnerlogo-Katalog

Jeder neue, technisch validierte Gegnerlogo-Upload wird zusätzlich als eigene
kanonische Datei in den systemweiten Katalog kopiert. Angezeigt werden dort nur
Vereinsname, Katalogversion und Prüfsumme. Quellverein, Benutzer und interne
Tenant-Pfade werden anderen Vereinen nicht offengelegt.

Wählt ein Verein ein Kataloglogo aus, prüft die Anwendung die kanonische Datei
und importiert sie als neue vereinsgebundene `LogoAsset`-Kopie. Spiel- und
Beitragssnapshots referenzieren damit weiterhin ausschließlich Assets des
eigenen Mandanten. Der globale Datensatz wird niemals direkt einem fremden
Spiel zugeordnet.

Nach dem ersten Deployment muss der vorhandene Bestand einmalig idempotent
übernommen werden:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.production.yml \
  exec -T web /app/scripts/entrypoint.sh \
  python scripts/shared_opponent_logo_catalog.py

docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.production.yml \
  exec -T web /app/scripts/entrypoint.sh \
  python scripts/shared_opponent_logo_catalog.py --apply
```

Der erste Lauf prüft nur. Der zweite legt fehlende kanonische Kopien an.
Fehlende, manipulierte oder unsichere Quelldateien werden gemeldet und nicht
übernommen. Wiederholungen erzeugen aufgrund von normalisiertem Namen und
SHA-256-Prüfsumme keine Dubletten.

