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

## Bestehende Beiträge vollständig trennen

**Spiele trennen** beendet auch die gemeinsame Beitragsverarbeitung. Bei bereits
verlegten oder früher nur optisch getrennten Spielen steht am alten gemeinsamen
Beitrag unter **Weitere Aktionen und Gefahrenbereich** die Aktion
**Spiele und offene Beiträge trennen** bereit. Sie berücksichtigt alle betroffenen
Mannschaften, auch wenn nur ein Teilbeitrag geöffnet wurde.

- Alle noch aktiven gemeinsamen Beiträge werden archiviert. Ihre Texte, Medien,
  eingefrorenen Versionen und veröffentlichten Plattformaufträge bleiben erhalten.
- Noch nicht gesendete gemeinsame Feed- und individuelle Story-Aufträge dieser
  Beiträge werden abgebrochen. Sie erscheinen nicht mehr als offene Freigaben.
- Wartende gemeinsame Generierungsaufträge werden abgebrochen. Laufende
  Generierungen, laufende/unklare Veröffentlichungen oder vorhandene unaufgelöste
  Meta-Container blockieren die Trennung vollständig, bis sie geklärt sind.
- Jedes Spiel erhält einen neuen Generierungsstand und bleibt von automatischer
  Bündelung ausgeschlossen. Unter **Spiele** können neue, unabhängige Beiträge
  erstellt werden; die aktivierte Automatik kann sie zum vorgesehenen Zeitpunkt
  ebenfalls einreihen. Bestehende Freigabe- und Kostenregeln gelten unverändert.
- Alte Generierungsaufträge und gespeicherte Bearbeitungs-/Freigabelinks können
  archivierte Beiträge nicht wieder aktivieren. Wiederholtes Trennen verändert
  bereits neu angelegte Einzelaufträge nicht.

Eine durch den Import oder die Spielverlegungsfunktion erkannte Verschiebung auf
einen anderen Berliner Kalendertag führt dieselbe Trennung aus, wenn ein aktiver
oder noch wartender gemeinsamer Beitrag existiert. Eine reine Uhrzeitänderung am
selben Tag löst die Gruppe nicht auf. Bereits vor dieser Korrektur verlegte Spiele
werden über die oben genannte Aktion am alten Beitrag bereinigt.

Die historische Darstellung bleibt bewusst gemeinsam: Ein bereits veröffentlichtes
Karussell lässt sich nicht rückwirkend in unabhängige Veröffentlichungen zerlegen.
Aktive Einzelbeiträge haben keine Verknüpfung zu diesem alten Bündel.
Die Änderung benötigt keine Datenbankmigration und führt bei der Trennung selbst
keine externen Provider- oder KI-Aufrufe aus.


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

