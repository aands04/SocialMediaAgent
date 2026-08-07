# Medienvarianten und Veröffentlichungsregeln – Bestandsanalyse und Umsetzungsplan

Stand: 2026-08-07

## 1. Bestand

### Medien und Versionen

- `Post.feed_path`, `Post.feed_version` und `PublicationJob.media_path` bilden die
  aktuell sichtbare Datei ab.
- Mehrere Feed-Bilder werden als `PublicationMediaItem` eines Karussell-Auftrags
  gespeichert. `position` ist dabei zugleich Ausgabereihenfolge und fachliche
  Zuordnung.
- Story-Bilder werden über `StoryRule.media_slot` wiederverwendet. Die Version
  steht nur im Dateinamen beziehungsweise im JSON-Design-Snapshot.
- Frühere Dateien bleiben in der Regel auf dem Datenträger, sind aber nicht als
  auswählbare, relationale Versionen modelliert.
- `Post.text` und `Post.text_version` kennen nur den aktuellen Text. Eine
  unveränderliche Historie fehlt.
- Eine Freigabe bindet `PublicationJob.approved_post_version`, `text_snapshot`
  und Dateipfade. Eine konkrete Medien- oder Textversions-ID wird nicht
  eingefroren.

### Generierungsaufträge

- `GenerationJob` ist persistent, mandantengebunden und idempotent.
- Neu- und Änderungsgenerierungen werden asynchron verarbeitet.
- Zielmedien werden bislang über `rerender_feed` und Story-Auftrags-IDs
  ausgewählt. Einzelne Feed-Ausgaben oder bestehende Varianten können dadurch
  nicht unabhängig adressiert werden.
- Kontingentreservierung, Prompt-Dispatch und technische Validierung existieren
  bereits und dürfen nicht umgangen werden.

### Regeln und Zeitplanung

- Grundregeln liegen als JSON in `Team.rules`.
- Story-Zeitpunkte liegen in `StoryRule`; Wochentagswerte und Zielwochentage
  sind JSON-Matrizen.
- Die aktuelle Formularvalidierung verlangt bei festen Wochentagsmodellen alle
  sieben Tage. Fehlt ein Eintrag zur Laufzeit, fällt die Berechnung teilweise
  auf relative Standardzeiten zurück.
- Erzeugungsanzahl, Medienausgabe und Veröffentlichungszeitpunkt sind dadurch
  nur teilweise getrennt.
- Spielverschiebungen werden über bestehende Scheduling-Services behandelt,
  besitzen aber keine normalisierte Herkunftsregel pro Veröffentlichungsslot.

### Berechtigungen und Mandanten

- `club_id` ist auf Post-, Job-, Medien-, Regel- und Promptentitäten vorhanden.
- Routen nutzen bestehende RBAC-/Mannschaftsprüfungen und CSRF-Schutz.
- Hintergrundjobs tragen `club_id`; direkte IDs dürfen nur zusammen mit dem
  Tenant-Kontext aufgelöst werden.
- Geschützte Plattform-Prompts liegen getrennt von Vereinsansichten. Diese
  Trennung bleibt für neue Vorschau- und Versionsansichten verbindlich.

## 2. Risiken der Umstellung

1. Bestehende Dateien dürfen weder verschoben noch überschrieben werden.
2. Bereits veröffentlichte oder freigegebene Aufträge müssen exakt dieselben
   Dateien und Texte behalten.
3. Karussellposition und Bildvariante dürfen nicht verwechselt werden.
4. Story-Regeln können dieselbe Bildausgabe mehrfach veröffentlichen.
5. Legacy-Snapshots enthalten unterschiedliche JSON-Formate.
6. Parallel laufende Worker dürfen keine doppelte Versionsnummer vergeben.
7. Bei fehlender Wochentagsregel darf keine erfundene Uhrzeit entstehen.
8. Migrationen müssen auf SQLite und PostgreSQL deterministisch laufen und bei
   nicht eindeutig zuordenbaren Daten sicher abbrechen.

## 3. Zielmodell

### Medien

`GeneratedMediaSlot` beschreibt die fachliche Ausgabe:

- Verein, Beitrag, Spiel und Mannschaft
- Medientyp (`feed` oder `story`)
- Feed-/Karussellposition beziehungsweise Story-Ausgabenummer
- Variantenindex
- Auswahlmodus (`auto_latest` oder `manual`)
- aktuell ausgewählte und neueste Version
- stabiler, innerhalb des Beitrags eindeutiger `slot_key`

`GeneratedMediaVersion` beschreibt eine unveränderliche Datei:

- fortlaufende Versionsnummer je Slot
- Dateipfad, Prüfsumme, MIME-Typ, Größe und Abmessungen
- technischer Validierungs- und Generierungsstatus
- Generierungsauftrag, Spielerbild und Ersteller
- nicht geheime Promptmetadaten sowie Logo-/Designreferenzen

`PostTextVersion` bildet dieselbe Historie für Begleittexte ab.

Veröffentlichungsaufträge erhalten zusätzlich die ausgewählte Text- und
Medienversions-ID. Karussellpositionen binden je `PublicationMediaItem` eine
Medienversion. Legacy-Pfade bleiben während der Übergangsphase erhalten.

### Regeln

`ContentRuleSet` trennt je Geltungsbereich und Beitragstyp:

- Vererbung (`club`, `team`, `game`)
- Erzeugungsanzahl für Feed und Story
- Freigabe-/Auswahlpolitik
- optimistische Version

`PublicationRuleSlot` beschreibt eine konkrete Veröffentlichung:

- Feed oder Story
- ausgewählte Medienausgabe/Variante
- Zeitmodell (`relative`, `weekday_fixed`, `result_detected`, `manual`)
- Bezug, Richtung, Abstand
- optional genau ein Spielwochentag mit Zieltag und Uhrzeit
- eindeutige Priorität und Aktivstatus

Fehlt eine passende Wochentagsregel, werden Medien weiterhin erzeugt, jedoch
keine Uhrzeit erfunden. Der Auftrag erhält den Zustand „Manuelle Planung
erforderlich“.

## 4. Migrationsstrategie

1. Neue Tabellen und nullable Fremdschlüssel in einer neuen Alembic-Migration
   anlegen.
2. Für jeden vorhandenen Post eine Textversion erzeugen.
3. Vorhandene Feed-/Karussell-/Story-Dateien als Slot 1 / Version 1 importieren.
4. Freigegebene Veröffentlichungsaufträge an exakt diese Versionen binden.
5. Eindeutig abbildbare Team- und Story-Regeln in strukturierte Regelsätze
   übernehmen. Unvollständige Sieben-Tage-Matrizen werden nur für tatsächlich
   konfigurierte Tage migriert.
6. Nicht eindeutig abbildbare Altwerte bleiben im bisherigen JSON erhalten und
   werden im Migrationsbericht ausgewiesen; sie werden nicht verworfen.
7. Alte Spalten und JSON-Felder zunächst nicht löschen. Der Downgrade entfernt
   ausschließlich die neue Schicht.

## 5. Implementierungsabschnitte

1. Datenmodelle, Constraints, Migration und Legacy-Backfill.
2. Zentraler Medienversionsservice mit transaktionssicherer Nummernvergabe,
   Auswahl und Publication-Freeze.
3. Integration in Erstellung, Neugenerierung, Textbearbeitung und Freigabe.
4. Regelauflösung mit `game > team > club`, ohne Zeit-Fallback bei fehlendem
   Wochentag.
5. Kompakte Beitragsprüfung mit Medienkarten, Varianten-/Versionsauswahl,
   Vergleich und gemeinsamer KI-Überarbeitung.
6. Neu strukturierte Regelansicht mit mehreren Veröffentlichungsslots,
   Zeitachsen-Vorschau, Kopieren und nachvollziehbarer Vererbung.
7. Tenant-, RBAC-, CSRF-, Parallelitäts-, Migrations- und Publishing-Tests.

## 6. Abnahmekriterien

- Eine neue Generierung überschreibt keine ältere Version.
- Während einer Generierung bleibt die bisher gewählte Version sichtbar und
  veröffentlichbar.
- Freigegebene Aufträge referenzieren konkrete unveränderliche Versionen.
- Jede Feed-Ausgabe und jede Story ist einzeln oder gesammelt auswählbar.
- Fehlende Wochentagsregeln erzeugen keine Ersatzzeit.
- Bereits veröffentlichte Dateien bleiben unverändert.
- Alle neuen Abfragen sind auf `club_id` und die vorhandenen Rechte begrenzt.
- Plattform-Prompts erscheinen weder in Vereins-HTML noch in Vereins-APIs.

## 7. Umgesetzte Bedienung und Kompatibilität

- Die Beitragsprüfung zeigt jede Feed-/Karussellposition und jede Story als
  eigene Ausgabe. Varianten und unveränderliche Versionen können verglichen
  und ohne neuen KI-Aufruf ausgewählt werden.
- Eine gemeinsame KI-Überarbeitung verwendet dasselbe persistente
  `GenerationJob`-Verfahren wie die Ersterzeugung. Zielausgaben und das
  Spielerbild je Mannschaft werden ausdrücklich ausgewählt; „alle“ und
  „Auswahl aufheben“ sind reine Bedienhilfen.
- Manuelle Auswahl bleibt bei späteren Generierungen bestehen. Erst das
  bewusste Zurückschalten auf „automatisch neueste Version“ folgt wieder neuen
  Ergebnissen.
- Die Regelansicht trennt Erzeugungsanzahl und Veröffentlichungsslots. Ein Slot
  kann relativ, an einem festen Spielwochentag, nach Ergebniserkennung oder
  manuell geplant werden. Regeln lassen sich auf Vereins-, Mannschafts- und
  Spielebene auflösen und kontrolliert kopieren.
- Legacy-Spalten und -JSON bleiben lesbar. Migration `0024` erzeugt daraus
  Textversionen, Medienslots, Medienversionen sowie eindeutig ableitbare
  Regelsätze, ohne alte Dateien zu verschieben oder zu löschen.
- Historische Medien- und Textversionen werden durch einen zentralen
  Session-Guard gegen nachträgliche Änderung geschützt. Bereits veröffentlichte
  Aufträge werden beim Synchronisieren niemals auf neue Dateien umgebogen.

### Bewusste Grenze

Die Karussellreihenfolge wird in der bestehenden Zwei-Mannschaften-Bündelung
über „erstes Bild festlegen“ gesteuert. Freies Drag-and-drop für beliebig
viele Positionen ist noch nicht Bestandteil dieser Ausbaustufe; die
persistente Positionsstruktur ist dafür vorbereitet.
