# Kompakte Spieleübersicht

Die Vereinsseite **Spiele** fasst Spiel, Beitragsstatus, automatische Erstellung und
Veröffentlichung in einer kompakten Spieltagsansicht zusammen. Die Darstellung ist
mandantengebunden und verwendet ausschließlich Mannschaften und Spiele, die der
angemeldete Benutzer sehen darf.

## Berechnung der automatischen Erstellung

Die Oberfläche berechnet keine eigenen Termine. Der FUSSBALL.DE-Worker und die
Spieleübersicht verwenden gemeinsam
`app.games.automatic.automatic_generation_candidates`. Damit stammen Aktivierung,
Beitragstypen und Fälligkeit aus derselben produktiven Logik.

- Ankündigungen und Erinnerungen zeigen den tatsächlich berechneten Zeitpunkt.
- Ergebnismeldungen sind ereignisbasiert und werden als „Nach bestätigtem
  Endergebnis“ angezeigt. Es wird keine fiktive Uhrzeit erzeugt.
- Bereits erzeugte Beiträge und laufende oder fehlgeschlagene Aufträge werden bei
  der Anzeige berücksichtigt.
- Nach einer Spielverlegung wird der Termin beim nächsten Seitenaufruf aus den
  aktuellen Spieldaten neu berechnet.

Die Route lädt Spiele, Beiträge, Generierungsaufträge, Story-Regeln,
Medienpräferenzen, Logos und Veröffentlichungsaufträge jeweils gesammelt. Das
ViewModel `GameAutomationSummary` führt diese bereits mandantengeprüften Daten
zusammen und führt keine Datensatzabfragen pro Spiel aus.

## Gemeinsame Spieltage

Bei verbundenen Spielen erscheint die Automatik- und Veröffentlichungsübersicht
einmal für den gesamten Spieltag. Abweichende Mannschaftsregeln bleiben unter den
Automatikdetails sichtbar. Die Bilderwahl, Ergebnisbestätigung und technischen
Spielaktionen bleiben weiterhin dem jeweiligen Spiel zugeordnet.

## Status

Die Oberfläche unterscheidet unter anderem:

- automatisch geplant
- wird gerade erstellt
- erstellt beziehungsweise veröffentlicht
- ereignisbasiert nach bestätigtem Ergebnis
- überfällig oder fehlgeschlagen
- manuelle Erstellung erforderlich, wenn kein automatischer Beitragstyp aktiviert ist
- nicht automatisch geplant
- Automatisierungszeit derzeit nicht bestimmbar

Ein fehlgeschlagener Generierungsauftrag wird über „Problem prüfen“ geöffnet. Eine
überfällige, noch nicht gestartete Erstellung kann bewusst sofort ausgelöst werden.

## Datenmodell und Kompatibilität

Für die Überarbeitung wurden keine neuen Tabellen oder Spalten benötigt. Bestehende
Spiele, Beiträge, Bündelungen, Zeitregeln und Veröffentlichungsaufträge bleiben
unverändert. Die manuelle Medienauswahl wird nur dann als manuell angezeigt, wenn
für das Spiel tatsächlich der Modus `manual` gespeichert ist.
