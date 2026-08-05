# Implementierungs- und Migrationsplan für die SaaS-Plattform

Dieser Plan teilt die Umstellung in rückwärtskompatible, einzeln testbare
Abschnitte. Jede Phase setzt die Sicherheitsinvarianten der vorherigen Phase
voraus.

## Phase 0 – Vorprüfung und Sicherheitsnetz

- Bestandsbericht und Migration-Preflight bereitstellen.
- Bestehende Vereinsnamen, Team-/Seitenbeziehungen, Benutzerrechte und globale
  Objekte prüfen.
- Produktivmigration bei Mehrdeutigkeit kontrolliert abbrechen.
- Bestehende Staging-, Produktions-, Backup- und Restorechecks erhalten.

## Phase 1 – Club-, Rollen- und Tariffundament

- `Club`, `PlanProfile`, versionierte Profilwerte, Club-Limitüberschreibungen,
  Zusatzkontingente und Feature Flags einführen.
- PlatformAdmin als getrennten Kontotyp einführen; Clubbenutzer behalten ihre
  Vereinsrollen.
- Bestehende Daten einem initialen Club zuordnen.
- Benutzer-/Club- und Team-/Club-Invarianten mit Constraints absichern.
- Sitzungsinvalidierung bei Status- oder Vereinswechsel implementieren.

## Phase 2 – TenantContext und Isolation

- unveränderlichen `TenantContext` und `PlatformContext` implementieren;
- Authentifizierung und Berechtigungen deny-by-default umstellen;
- tenantgebundene Repository-Helfer bereitstellen;
- Routen, Services, Jobs, Scheduler, Downloads, Audit und Monitoring auf
  explizite Kontexte umstellen;
- Cross-Tenant-Tests für jede Entitätsfamilie ergänzen.

## Phase 3 – PlatformAdmin- und Vereinsverwaltung

- getrennten `/platform`-Bereich mit eigener Rollenprüfung erstellen;
- transaktionssichere Vereinsanlage inklusive erstem Clubadministrator;
- Statuswechsel, Benutzertransfer, Sitzungsinvalidierung und Audit umsetzen;
- Tarifprofile, Limits, Feature Flags, Suche, Filter und CSV-Export ergänzen.

## Phase 4 – Effektive Limits

- Herkunft effektiver Limits aus Profil, Override und Zusatzkontingent
  berechnen;
- Mannschafts-, Instagram-Seiten- und Schriftartenlimits atomar prüfen;
- Clubstatus und Limits an allen mutierenden Servicegrenzen erzwingen;
- Warnschwellen und Clubdashboard ergänzen.

## Phase 5 – Providerunabhängiger Objektspeicher

- objektbasierten Storagevertrag ergänzen;
- Local-, S3-, Cloudflare-R2-, Hetzner- und SMB-Importprovider implementieren;
- alle privaten Objekte unter `clubs/{club_uuid}/...` ablegen;
- temporäre Publishing-Objekte unter `publishing/{club_uuid}/...` trennen;
- bestehende lokale Pfade über einen kontrollierten Legacyadapter weiter lesen.

## Phase 6 – Storage-Ledger und direkte Uploads

- Storage-Objekte, Reservierungen und Ledgerstatus modellieren;
- signierte Upload-URLs nur nach Tenant-, Typ- und Quotenprüfung ausstellen;
- Abschlussvalidierung und Bereinigung abgebrochener Uploads implementieren;
- Reconciliation zwischen Datenbank und Provider ergänzen.

## Phase 7 – Usage-Ledger und KI-Kontingente

- monatliche UTC-basierte Perioden mit Anzeige in Clubzeitzone modellieren;
- Text- und Bildkontingente atomar reservieren;
- Anbieteraufruf, technische Validierung, Bereitstellung, Ablehnung, Refund und
  interne Plattformkosten idempotent verbuchen;
- laufende Reservierungen bei Neustart wiederaufnehmen oder freigeben.

## Phase 8 – Geschütztes Prompt- und Brandingsystem

- globale Prompts in ausschließlich PlatformAdmin-sichtbare Versionen
  überführen;
- Clubanpassungen und strukturierte Branding-/Textkonfiguration ergänzen;
- Prompttexte aus Clubantworten, Templates, Audit und Design-Snapshots entfernen;
- serverseitige Komposition mit unveränderlichen Versionsreferenzen umsetzen;
- Prompttests als nicht club-abrechenbare Plattformnutzung erfassen.

## Phase 9 – Selbstregistrierungs- und Billing-Fundament

- Registrierungs-, Einladungs-, Vertrags- und Abonnementstatus modellieren;
- `BillingProvider` und `MockBillingProvider` definieren;
- öffentliche Selbstregistrierung und echte Zahlung per Feature Flag deaktiviert
  lassen;
- nur PlatformAdmin-Clubanlage produktiv freischalten.

## Phase 10 – Verifikation und Rollout

- vollständige SQLite-Kompatibilitätstests ausführen;
- PostgreSQL-Isolation, Constraints, Locks und Parallelität separat testen;
- Upgrade/Downgrade und Migrations-Preflight mit Kopie produktionsnaher Daten
  prüfen;
- Docker-Compose-Konfigurationen und Betriebschecks prüfen;
- Datenexport, Restore, Archivierung und Rollback dokumentieren;
- produktiven Rollout zunächst mit deaktivierten SaaS-Feature Flags durchführen.

## Migrationsstrategie für die vorhandene Installation

Die Migration erhält explizite Parameter für den initialen Club (UUID, Name,
Kurzname und Slug). Ohne diese Parameter darf eine Datenbank mit Bestandsdaten
nicht automatisch migriert werden. Eine separate Preflight-Ausgabe listet:

- gefundene Clubnamen;
- Datensätze je Entitätsfamilie;
- globale Datensätze ohne Teambezug;
- widersprüchliche Team-/Instagram-Zuordnungen;
- verwaiste Fremdschlüssel;
- erwartete Club-ID;
- geplante Klassifizierung globaler Einstellungen und Vorlagen.

Nur eine eindeutige Vorprüfung erlaubt den transaktionalen Backfill. Der Bericht
wird mit Prüfsumme und Zeitstempel gespeichert. In der Anwendung selbst gibt es
keinen impliziten initialen oder Standardclub.

## Rollout-Gates

- `MULTI_TENANT_ENABLED=false` bleibt bis zum erfolgreichen Datenbank- und
  Isolationstest gesetzt.
- `SELF_REGISTRATION_ENABLED=false` und `BILLING_ENABLED=false` bleiben in
  dieser Iteration zwingend deaktiviert.
- S3 wird für SaaS-Deployments verlangt; lokale bestehende Installationen dürfen
  über den Legacyadapter weiterbetrieben werden.
- Automatische Generierung und Veröffentlichung bleiben für gesperrte,
  archivierte oder über dem Limit liegende Clubs serverseitig blockiert.
