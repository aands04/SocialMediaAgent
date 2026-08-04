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
2. Eine Zuordnung aus Spiel-Wochentag, Veröffentlichungs-Wochentag und fester
   Berliner Uhrzeit. Beispielsweise kann ein Sonntagsspiel am vorherigen
   Freitag um 18:00 Uhr und ein Freitagsspiel am vorherigen Donnerstag um
   15:00 Uhr angekündigt werden.

Ergebnis-Feeds unterstützen zusätzlich „sofort nach bestätigter Erkennung“.
In diesem Modus ist der erkannte Zeitpunkt wörtlich der Veröffentlichungstermin;
eine eventuell für geplante Ergebniszeiten hinterlegte Wartezeit wird ignoriert.
Feed und Ergebnis-Story werden gemeinsam zu diesem Zeitpunkt eingeplant. Bei
automatischer Freigabe übernimmt der Scheduler beide sofort. Ohne automatische
Freigabe setzt die spätere manuelle Freigabe einen bereits verstrichenen
Sofort-Termin auf den aktuellen Zeitpunkt.
Ein konfigurierter Ergebniszeitpunkt wird niemals vor der bestätigten
Ergebniserkennung verwendet. Bei festen Wochentagen liegt die Ankündigung auf
dem gewählten Tag vor oder am Spieltag; für Ergebnisse wird der gewählte Tag
nach oder am Spieltag verwendet.

Story-Regeln können weiterhin relativ geplant werden oder eine eigene feste
Uhrzeit je Spiel-Wochentag erhalten. Feste Zeiten werden als absolute Termine
markiert, sodass spätere Spielverlegungen die vorhandenen Schutzmechanismen für
veraltete Termine auslösen.

## Gemeinsame Feeds mehrerer Vereinsmannschaften

Für aktive Mannschaften desselben Vereins und derselben Instagram-Seite kann
unter **Regeln & Storys** gewählt werden:

- alle Feed-Beiträge getrennt,
- nur Ankündigungen als gemeinsames Karussell,
- Ankündigungen und Ergebnisse als gemeinsame Karussells.

Treffen mindestens zwei so konfigurierte Mannschaften am selben Berliner
Kalendertag an, wartet der Feed bis alle zugehörigen Einzelgrafiken vorhanden
sind. Danach wird genau ein gemeinsamer Feed-Auftrag mit einer Grafik pro Spiel
und einem sachlich zusammengefassten Begleittext erstellt. Die Storys bleiben
immer je Spiel und Mannschaft getrennt.

Gemeinsame Ergebnis-Feeds warten zwangsläufig auf die bestätigten Ergebnisse
aller beteiligten Spiele. Wer Ergebnisse wirklich unmittelbar nach jedem
Abpfiff veröffentlichen möchte, wählt deshalb **nur Ankündigungen als
gemeinsames Karussell**; Ergebnis-Feeds und Ergebnis-Storys bleiben dann
vollständig ad hoc.

Eine automatische Freigabe des gemeinsamen Feeds erfolgt nur, wenn sie bei
allen beteiligten Mannschaften aktiviert ist. Änderungen, Absagen oder Sperren
eines enthaltenen Spiels entziehen die Freigabe des gesamten offenen
Karussells. Vor dem Publisher-Aufruf werden alle enthaltenen Spiele erneut
geprüft.

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
