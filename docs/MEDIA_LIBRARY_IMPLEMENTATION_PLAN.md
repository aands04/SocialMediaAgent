# Medienbibliothek und Bildauswahl – Analyse und Umsetzung

Stand: 11. August 2026

## Bestandsanalyse

Vor der Erweiterung verwaltete die Medienbibliothek hochgeladene und extern importierte
Bilder in `MediaAsset`. Der Datensatz enthält bislang Mannschaft, Speicherpfad,
Dateiinformationen sowie die Felder `active`, `available`, `reserved_game_id`
und `uses`. Diese vier Felder vermischen technische Verfügbarkeit,
automatische Auswahl, Reservierung und Verbrauch. Eine Medienkategorie, eine
nachvollziehbare Nutzungshistorie, vereinsbezogene Auswahlrichtlinien und eine
explizite Bildauswahl je Spiel fehlten.

Die automatische Auswahl erfolgt in `app.posts.service.reserve_image`: Sie
wählt das größte noch nie verwendete, aktive und nicht reservierte Bild einer
Mannschaft. Beitragstyp und Vereinsbranding werden dabei nicht berücksichtigt.
Die SQL-Abfrage verwendet unter PostgreSQL bereits `FOR UPDATE SKIP LOCKED` und
bildet damit eine gute Grundlage für parallele Worker.

Die Seite `/media` ist mandanten- und mannschaftsgebunden, zeigt jedoch eine
technische Tabelle mit Quelle, Bytes, Prüfsumme, Nutzungszähler und interner
Reservierungs-ID. Uploads werden bereits automatisch im unveränderlichen
Vereins-/Mannschaftsnamespace gespeichert. SMB beziehungsweise ein lokaler
externer Ordner ist nur eine optionale Importquelle.

Der bestehende `TenantContext`, die SQLAlchemy-Loader-Kriterien und der
Schreibschutz in `TenantSession` verweigern unklare oder widersprüchliche
Mandantenzugriffe. Diese Schutzschicht wird für alle neuen Tabellen, Dienste,
Routen und Downloads beibehalten. Die vorhandenen Rollen- und
Mannschaftsrechte werden weiterhin über `require` geprüft.

## Datenübernahme

- Alle vorhandenen Medien werden ohne Dateioperation als `match_photo`
  (Spielbild) klassifiziert.
- Vorhandene Reservierungen und Nutzungszähler bleiben erhalten.
- Ein bereits verwendetes Bild wird standardmäßig von der weiteren
  automatischen Auswahl ausgeschlossen; es kann bewusst global freigegeben
  oder ausschließlich für ein konkretes Spiel wiederverwendet werden.
- Alte Freitextangaben zum Spieler bleiben in `player_name` erhalten.
- Keine bestehende Migration wird verändert. Die Erweiterung beginnt mit
  Revision `0028`.

## Umgesetztes Zielmodell

1. `MediaAsset` erhält Kategorie, optionale Spielzuordnung, Beschreibung,
   Aufnahmedatum, Fotograf, Upload-Benutzer, Automatikfreigabe und Soft-Delete.
2. `MediaUsageHistory` protokolliert jede Reservierung, Verwendung,
   Freigabe, manuelle Wiederverwendung und Löschung unveränderlich.
3. `ClubMediaUsagePolicy` definiert je Beitragstyp erlaubte Kategorien und
   deren Priorität. Sichere Voreinstellung für Ankündigung, Erinnerung und
   Ergebnis: ausschließlich Spielbilder.
4. `GameMediaPreference` speichert die automatische oder bewusste Auswahl je
   Spiel, Mannschaft und Beitragstyp. Die Auswahl wird serverseitig und
   mandantengebunden validiert.

## Auswahlreihenfolge

1. explizite, gültige Spielauswahl,
2. künftig mögliche Mannschaftsüberschreibung,
3. aktive Vereinsrichtlinie,
4. sichere Standardrichtlinie.

Innerhalb der ersten erlaubten Kategorie mit verfügbaren Kandidaten wird unter
PostgreSQL zufällig und mit Zeilensperre ausgewählt. Zulässig sind nur Medien
desselben Vereins und derselben Mannschaft, deren Datei verfügbar, nicht
gelöscht, für Automatik freigegeben, nicht reserviert und nicht verbraucht ist.
Eine explizite Auswahl kann nach bestätigter Warnung eine deaktivierte Kategorie
oder ein bereits verwendetes Bild genau für dieses Spiel verwenden.

## Oberfläche

- Die Medienbibliothek ist eine responsive Galerie mit Filtern, verständlichen
  Statusangaben, Detailansicht und geführtem Upload.
- Technische Speicherwerte bleiben in einem eingeklappten Administrationsblock.
- Mehrfachaktionen unterstützen Freigabe, Kategorie, Mannschaft und sichere
  Löschung.
- Die Spielseite verlinkt auf eine Auswahlseite mit „Automatisch auswählen“ und
  einer bewussten konkreten Bildauswahl je Beitragstyp.
- Die Vereinsbranding-Seite verwaltet erlaubte Kategorien und Prioritäten je
  Beitragstyp; zentrale geschützte Prompts werden dabei weder gelesen noch an
  den Browser übertragen.

## Sicherheits- und Konsistenzregeln

- Jede Abfrage und Mutation enthält den aktuellen `club_id`-Kontext.
- Medien-, Spiel- und Mannschaftszuordnungen werden gemeinsam validiert.
- Auswahl und Reservierung erfolgen transaktional mit Versions- und
  Parallelitätsschutz.
- Nach vorhandener Generierung ändert eine neue Präferenz ausschließlich
  zukünftige Generierungen. Eine kostenpflichtige Neugenerierung bleibt eine
  getrennte, ausdrückliche Aktion.
- Historische generierte Dateien, Beiträge und Veröffentlichungen werden durch
  Medienlöschung nicht verändert.
- Audit-Einträge enthalten verständliche fachliche Änderungen, aber keine
  vertraulichen Speicherpfade oder Prüfsummen.

## Prüfschritte

Neben der vollständigen Testreihe werden gezielt Migration, Kategorien,
Upload, Filter, Bulk-Aktionen, Richtlinien, manuelle Auswahl,
Wiederverwendungsbestätigung, Tenant-Isolation und PostgreSQL-Parallelität
geprüft. Zusätzlich decken Tests das Storage-Ledger, die physische Löschung
unbenutzter Uploads, die Ablehnung fremder Mannschaftsmedien und direkte
Cross-Tenant-URLs ab. Danach folgen Ruff, Compileall, Alembic
Upgrade/Downgrade, Compose-Konfiguration und `git diff --check`.

## Speicher und Quoten

Neue Dashboard-Uploads reservieren vor dem Schreiben Speicher im bestehenden
`StorageLedgerEntry`. Nach erfolgreicher Bildprüfung werden tatsächliche Größe,
Prüfsumme, MIME-Typ und der tenantgebundene Objektschlüssel als
`StorageObject` bestätigt. Bereits vor dieser Erweiterung vorhandene
`MediaAsset`-Dateien werden bei der Verbrauchsberechnung mitgezählt, solange
noch kein zugehöriges Storage-Objekt existiert. Beim physischen Löschen eines
unbenutzten Uploads werden Storage-Objekt und Ledger nachvollziehbar als
gelöscht markiert. Verwendete Medien bleiben als historische Referenz und
Speicherbelegung erhalten.

## Migration

Alembic-Revision `0028` ergänzt ausschließlich neue Felder und Tabellen. Alle
Bestandsmedien werden als Spielbild eingestuft; verwendete Medien werden aus
der automatischen Auswahl genommen. Dateipfade und Dateien werden nicht
verschoben. Bestehende Nutzungszähler und Reservierungen bleiben erhalten. Die
Migration erzeugt außerdem die sicheren Standardrichtlinien für jeden Verein.

## Verbleibende Grenze

Das Projekt besitzt noch kein eigenes Spielermodell. Einzelfotos behalten
daher den optionalen, validierten Namen in `MediaAsset.player_name`; es wurde
für diese Erweiterung bewusst keine funktionslose Spielerverwaltung angelegt.
