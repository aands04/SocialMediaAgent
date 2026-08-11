# Veröffentlichungsarbeit

## Zielbild

Die operative Arbeit mit Beiträgen ist von der technischen Zustellung getrennt:

- **Vereinsübersicht:** kompakter Ausblick auf den nächsten Termin, geplante Vorgänge, Handlungsbedarf und eingerichtete Kanäle.
- **Spiele & Testdaten:** sportliche Sicht, nach Spieltag gruppiert. Gemeinsame Spieltagsbeiträge erscheinen als eine Einheit.
- **Beiträge & Freigaben:** zentrale Arbeitsansicht mit den Fragen *Was?*, *Wann?* und *Wo?* sowie Filtern nach Zeitraum, Kanal, Status, Mannschaft und Inhalt.
- **Beitragsprüfung:** fachliche Bearbeitung und Freigabe mit den tatsächlich geplanten Kanalzielen.
- **Veröffentlichungen:** technische Historie für Zustellversuche, Plattform-IDs, Fehlermeldungen und Abbruchaktionen.

Damit ist `/posts` die zentrale operative Ansicht. `/publications` ist bewusst keine zweite Arbeitsliste.

## Zentrale Darstellungslogik

`app/publishing/presentation.py` übersetzt die technischen Zustände der
Veröffentlichungsaufträge in ein gemeinsames, deutschsprachiges View-Modell.
Dashboard, Spieleliste, Beitragsübersicht, Beitragsdetail und technische Historie
verwenden damit dieselben Bezeichnungen und Zielinformationen.

Die Darstellung enthält nur eingerichtete Kanäle. Als konkretes Ziel werden zum
Beispiel der Instagram-Benutzername, der Name der Facebook-Seite oder die
eingerichtete WhatsApp-Verbindung angezeigt. Interne Statuswerte bleiben in den
operativen Ansichten verborgen; in der technischen Historie sind sie ausschließlich
in eingeklappten Details sichtbar.

## Mandanten- und Mannschaftsschutz

Jeder Aufruf der zentralen Darstellungslogik benötigt eine ausdrückliche `club_id`.
Ein fehlender Vereinskontext wird abgewiesen. Abfragen für Beiträge, Spiele,
Mannschaften, Medien und Kanalverbindungen enthalten zusätzlich den Verein als
Filter. Die Routen reduzieren die Ergebnisse danach auf die für den angemeldeten
Benutzer sichtbaren Mannschaften.

Kanalfilter akzeptieren nur IDs aus der zuvor tenantgebunden geladenen Liste.
Manipulierte Vereins-, Mannschafts- oder Kanal-IDs werden nicht als Fallback auf
einen Standardverein aufgelöst.

## Zeiträume und Status

Standardmäßig zeigt die Arbeitsansicht:

- abgeschlossene Veröffentlichungen und Sendungen der letzten zwei Tage,
- anstehende Vorgänge der nächsten sieben Tage,
- offene oder überfällige Vorgänge mit Handlungsbedarf.

Der Vorschauzeitraum kann zwischen einem und 90 Tagen gewählt werden. Zeitwerte
werden intern als UTC behandelt und in der Vereinsoberfläche in der Berliner
Zeitzone dargestellt. Auch aus SQLite geladene Zeitwerte ohne Zeitzoneninformation
werden sicher als UTC interpretiert.

## Performance

Die Präsentationslogik lädt Beiträge, Spiele, Mannschaften und Karussellmedien
jeweils gesammelt. Kanalzuordnungen werden einmal indiziert. Dadurch entstehen
keine Einzelabfragen pro sichtbarem Veröffentlichungsauftrag.

## Datenmodell und Migration

Für diese UX-Überarbeitung wurde keine Datenbankmigration benötigt. Es werden die
bestehenden mandantengebundenen Modelle `PublicationJob`, `Post`, `Game`, `Team`,
`SocialChannelConnection` und `PublicationMediaItem` verwendet. Historische Daten
und bisherige Veröffentlichungsabläufe bleiben unverändert.

