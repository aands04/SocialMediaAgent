# Mannschaftsbezogene FUSSBALL.DE-Planung

Die globalen Produktions-Gates `FUSSBALL_AUTOMATIC_SYNC_ENABLED` und
`AUTOMATIC_POST_GENERATION_ENABLED` bleiben Voraussetzung. Darunter wird der
Ablauf unter **Regeln & Storys** pro Mannschaft konfiguriert.

## Abruf und Ergebnissuche

- Der normale Spielplanabruf läuft standardmäßig alle 24 Stunden. Neue Spiele
  werden angelegt; vorhandene Spiele werden anhand ihrer Provider-ID
  idempotent aktualisiert.
- Am lokalen Spieltag (`Europe/Berlin`) wechselt die Mannschaft automatisch in
  ein eigenes Ergebnisintervall, standardmäßig 15 Minuten.
- Der nächste Spieltag wird spätestens um Mitternacht fällig. Damit hängt das
  15-Minuten-Raster nicht von der Uhrzeit des vorherigen Tagesabrufs ab.
- Ergebnisse gelten weiterhin erst nach den bestehenden Stabilitätsprüfungen
  als bestätigt. Abweichende oder unklare Werte werden nicht geraten.

## Automatische Generierung und Freigabe

Der Generierungsvorlauf wird in Tagen vor dem Anpfiff festgelegt. Der Standard
ist vier Tage. Eine Erstellung umfasst den Feed und alle aktiven Story-Regeln
des Beitragstyps. Bestätigte Ergebnisse werden ohne zusätzlichen Vorlauf sofort
als Generierungsjob eingereiht.

Automatische Freigaben sind getrennt für Ankündigungen und Ergebnisse
aktivierbar. Ohne diese Opt-ins bleibt jeder Beitrag freigabepflichtig. Mit
Opt-in wird nach erfolgreicher Generierung derselbe Freigabeservice verwendet
wie im Dashboard. Fehlende Logos, Medien, Rechte, Versionen oder eine inaktive
Instagram-Verbindung blockieren die Freigabe weiterhin und werden auditiert.

Bei aktivierter Instagram-Automatik kann eine automatisch freigegebene Ausgabe
zum geplanten Zeitpunkt ohne weiteren Klick veröffentlicht werden. Deshalb
sollte diese Option erst nach einem vollständigen Testwochenende eingeschaltet
werden.

## Veröffentlichungszeiten

Für Ankündigungs-Feeds stehen zwei Modelle zur Verfügung:

1. Abstand in Minuten vor oder nach dem Anpfiff.
2. Eine feste Berliner Uhrzeit für jeden Wochentag, an dem das Spiel stattfindet.

Ergebnis-Feeds unterstützen zusätzlich „sofort nach bestätigter Erkennung“.
Ein konfigurierter Ergebniszeitpunkt wird niemals vor der bestätigten
Ergebniserkennung verwendet.

Story-Regeln können weiterhin relativ geplant werden oder eine eigene feste
Uhrzeit je Spiel-Wochentag erhalten. Feste Zeiten werden als absolute Termine
markiert, sodass spätere Spielverlegungen die vorhandenen Schutzmechanismen für
veraltete Termine auslösen.

## Empfohlene erste Konfiguration

- Spielplanabruf: 24 Stunden
- Ergebnisprüfung am Spieltag: 15 Minuten
- Generierung: 4 Tage vor Anpfiff
- Ergebnis-Wartezeit nach bestätigter Erkennung: 0 Minuten
- Automatische Generierung: aktiv
- Automatische Freigabe: zunächst aus
- Feed: relativ, z. B. 1440 Minuten vor Anpfiff

Nach der Abnahme können automatische Freigaben einzeln aktiviert werden.
Absagen, Verlegungen, Import-Sperren und globale Not-Aus-Gates bleiben immer
vorrangig.
