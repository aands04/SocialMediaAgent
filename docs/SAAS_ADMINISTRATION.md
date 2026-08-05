# SaaS-Administration

## Kontotypen und Rollen

`PlatformAdmin` ist ein eigener Kontotyp ohne Vereinszuordnung und wird nur mit
`python scripts/platform_admin.py ...` beziehungsweise im geschützten
Plattformbereich verwaltet. Ein Vereinsadministrator kann diesen Kontotyp nicht
vergeben.

Vereinskonten besitzen genau eine `club_id`. Die Rollen lauten:

- **Vereinsadministrator**: Clubkonfiguration und Benutzerverwaltung;
- **Redakteur**: Beiträge erstellen, bearbeiten und freigeben;
- **Autor**: Beiträge erstellen und bearbeiten, nicht freigeben;
- **Freigeber**: prüfen und freigeben, nicht neu erstellen;
- **Nur Lesen**: ausschließlich lesender Zugriff.

Mannschaftsrechte gelten immer zusätzlich und nur innerhalb des eigenen Clubs.
Beim Plattformwechsel eines Benutzers werden `auth_version`, Sessions und
unzulässige Teamzuweisungen invalidiert und der Vorgang plattformweit auditiert.

## Vereine und Status

Vereine werden transaktionssicher unter `/platform/clubs` gemeinsam mit ihrem
ersten Administrator angelegt. `setup_pending` erlaubt die kontrollierte
Einrichtung, aber noch keine kostenpflichtige Generierung oder Veröffentlichung.
`trial` und `active` erlauben den Betrieb. `suspended`, `cancelled` und
`archived` blockieren neue Uploads, Generierungen, Veröffentlichungen,
Mannschaften und automatische Jobs, löschen jedoch nichts. Lesende Ansichten
bleiben für berechtigte Vereinsbenutzer verfügbar.

Archivierung beendet aktive Sitzungen und blockiert geplante externe Arbeit.
Die endgültige Löschung ist absichtlich nicht produktiv aktiviert. Vor einer
späteren Löschung sind Export, Wartefrist, offene Veröffentlichungen und
Aufbewahrungspflichten zu prüfen.

## Profile und Limits

Profile werden versioniert. Effektiv gilt in dieser Reihenfolge:

1. Wert aus dem Tarifprofil;
2. optionaler Club-Override;
3. aktive zeitlich begrenzte Zusatzkontingente.

Die Detailansicht zeigt Wert und Herkunft. Wird ein Limit unter den aktuellen
Verbrauch reduziert, bleiben Daten erhalten; nur neue limitrelevante Aktionen
werden blockiert. Mannschafts-, Instagram-Seiten- und Schriftartenlimits werden
innerhalb einer Sperrtransaktion geprüft.

## Prompt- und Feature-Verwaltung

Zentrale Prompts und Clubanpassungen sind nur für PlatformAdmins sichtbar.
Aktive Vorlagen werden nicht überschrieben; Änderungen erzeugen neue Versionen.
Fixturetests vergleichen Versionen ohne externe Anbieteraufrufe und belasten
kein Clubkontingent. Feature Flags besitzen entweder Plattform- oder Clubscope.

Verbrauchsdaten können über `/platform/usage.csv` exportiert werden.

## Speicherabgleich

Die Plattformübersicht bietet für PlatformAdmins einen manuellen Abgleich des
privaten Objektspeichers. Er kann für einen einzelnen Verein oder für alle
Vereine ausgeführt werden und vergleicht aktive `StorageObject`-Metadaten mit
dem konfigurierten Provider. Angezeigt werden fehlende, unerwartete und in der
Größe abweichende Objekte.

Der Abgleich ist absichtlich rein lesend: Er verändert keine Metadaten und
löscht oder überschreibt keine Objekte. Die Ausführung wird mit sicheren
Summenwerten im Plattform-Audit protokolliert; konkrete Objektschlüssel bleiben
im geschützten Reconciliation-Bericht. Abweichungen müssen vor einer manuellen
Korrektur untersucht und durch einen erneuten Abgleich bestätigt werden.
