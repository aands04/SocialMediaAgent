# Automatische Beiträge – Architektur und UX

## Bestandsanalyse

- Die bisherige Seite `/rules` mischte Inhaltserstellung, Veröffentlichung, FUSSBALL.DE,
  Vereinsstil und technische Regelparameter in einer langen Maske.
- Einstellungen liegen abwärtskompatibel in `Team.rules`. Beim Speichern erzeugt
  `sync_team_rule_sets()` daraus versionierte `ContentRuleSet`- und
  `PublicationRuleSlot`-Datensätze.
- Die produktive Terminberechnung erfolgt zentral in
  `app.posts.rules.calculate_publication_time()`. Fehlende Wochentagsregeln liefern
  bewusst keinen Ersatztermin und führen zur manuellen Planung.
- Schreibzugriffe sind durch Sitzung, CSRF, Vereinsadministratorrolle,
  Mannschaftssichtbarkeit, `club_id` und optimistische Versionen geschützt.
- Automatische Freigaben werden von der Generierungs- und Publishing-Pipeline erneut
  gegen Medien-, Freigabe-, Versions-, Vereins- und Instagram-Sicherheitsgates geprüft.
- Die bisherige Untergrenze für den Ergebnisabruf betrug fünf Minuten und war damit
  niedriger als die betriebliche Sicherheitsgrenze.

## Umsetzung

- Die Oberfläche heißt „Automatische Beiträge“ und erläutert Feed, Story,
  Varianten, manuelle Planung und gemeinsame Feed-Beiträge in Alltagssprache.
- Das zentral definierte, versionierte Preset `safe-club-automation` kann nach einer
  Vorschau entweder nur fehlende Regeln ergänzen oder nach zweiter Bestätigung
  ersetzen. Geschützte Prompt- und bestehende Brandingwerte bleiben erhalten.
- Die Zeitplanungsvorschau ist rein lesend und verwendet dieselbe
  `calculate_publication_time()`-Funktion wie die Produktion. Sie erzeugt weder
  Beiträge noch Jobs, Kontingentbuchungen oder Spielplandaten.
- Ergebnisabrufe unter zehn Minuten werden im Formular, im Serverendpunkt, im Preset
  und bei der Vorschau als ungültig behandelt. Alte Werte bleiben sichtbar, müssen
  aber beim nächsten Speichern korrigiert werden.
- Automatische Freigaben bleiben standardmäßig aus. Ihre erstmalige Aktivierung
  erfordert eine gesonderte Bestätigung und wird separat auditiert.
- Redakteure und andere Vereinsrollen dürfen die Konfiguration lesen, aber nur
  Vereinsadministratoren dürfen Presets oder Regeln verändern. Sämtliche
  Schreibwege behalten CSRF-, Vereins-, Mannschafts- und Versionsprüfung bei.
- Nicht eingerichtete Spiel-Wochentage sind ausdrücklich zulässig: Die Medien
  werden vorbereitet und anschließend manuell terminiert. Regelkarten können
  einzeln angelegt, bearbeitet, gelöscht oder kontrolliert auf andere Spieltage
  kopiert werden.
- Der frühere Mannschafts-„Vereinsstil“ wird auf dieser Seite weder angezeigt noch
  verarbeitet. Vorhandene Daten werden nicht gelöscht; Branding wird ausschließlich
  unter `/branding` gepflegt.

## Empfohlenes Preset Version 1

- Spielankündigungen: zwei Feed- und vier Story-Varianten erzeugen; standardmäßig
  einen Feed und zwei Storys einplanen.
- Samstagsspiel: Feed Donnerstag 18:00 Uhr, Storys Freitag 18:00 Uhr und Samstag
  10:00 Uhr.
- Sonntagsspiel: Feed Freitag 18:00 Uhr, Storys Samstag 18:00 Uhr und Sonntag
  10:00 Uhr.
- Ergebnismeldungen: ein Feed und eine Story direkt nach bestätigter Erkennung.
- FUSSBALL.DE: Spielplan alle 24 Stunden, Ergebnisprüfung alle 15 Minuten,
  Vorbereitung vier Tage vor dem Spiel; vorläufige Spiele sind zulässig.
- Freigaben bleiben manuell; Erinnerungsbeiträge sind deaktiviert; gemeinsame
  Spieltage werden für Feed-Beiträge gebündelt.

Das Preset ist serverseitig zentral definiert und versioniert. Promptzuweisungen
und vorhandene Brandingwerte werden bei beiden Übernahmemodi nicht überschrieben.
„Nur Fehlendes ergänzen“ erhält individuelle Regeln. „Vorhandene Regeln ersetzen“
erfordert eine zweite Bestätigung und erzeugt einen Audit-Eintrag.

## Zeitplanung testen

`POST /rules/{team_id}/schedule-preview` ist ein geschützter, schreibfreier
Vorschau-Endpunkt. Er akzeptiert einen Beispiel-Anpfiff und optional einen
Ergebniszeitpunkt, berechnet daraus eine deutschsprachige Zeitleiste und meldet
unter anderem fehlende Spieltagsregeln, ungültige Varianten, Termine vor der
Medienerstellung und kollidierende Veröffentlichungen gleichen Formats. Dynamische
Texte werden im Browser vor der Darstellung escaped. Die Vorschau schreibt keine
Spiele, Beiträge, Jobs, Reservierungen oder Kontingentbuchungen.

## Migrationswirkung

Es ist keine Schemaänderung erforderlich. Bestehende `Team.rules`-Werte und alle
versionierten Regelstände bleiben lesbar. Das Preset schreibt ausschließlich über die
vorhandene Versionierungsfunktion neue Regelstände.
