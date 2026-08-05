# Backup und Restore der SaaS-Plattform

Ein konsistentes Backup besteht aus PostgreSQL-Dump, privatem Objektspeicher
beziehungsweise dessen versioniertem Backup, Secret-Inventar (nicht im Dump)
und Deploymentkonfiguration. Storage- und Usage-Ledger gehören zwingend zum
Datenbankbackup.

## Backup

1. Schreibende Worker kontrolliert pausieren.
2. Datenbankdump mit Transaktionssnapshot erstellen.
3. Bucketversionierung/Providerbackup und Lifecycle-Konfiguration sichern.
4. Checksummen, Schema-Revision und Objektanzahl dokumentieren.
5. Secrets getrennt verschlüsselt sichern; niemals in Anwendungsarchive legen.
6. Worker wieder starten.

## Restore

1. in isolierte Umgebung wiederherstellen;
2. Alembic-Revision prüfen;
3. Objektzugriff mit read-only Credentials prüfen;
4. Storage-Reconciliation pro Club ausführen;
5. Tenant-Isolation, Quoten und ein Dry-Run-Publishing prüfen;
6. erst danach DNS beziehungsweise Worker freigeben.

Ein Clubexport ist kein vollständiges Plattformbackup. Abweichungen zwischen
Ledger und Bucket dürfen nicht durch bloßes Ändern eines Summenzählers kaschiert
werden; Korrekturen benötigen einen nachvollziehbaren Ledger-/Audit-Eintrag.
