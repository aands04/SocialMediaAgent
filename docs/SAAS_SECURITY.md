# Sicherheitskonzept der mandantenfähigen Plattform

## Deny by default

Ein Clubzugriff benötigt einen authentifizierten Vereinsbenutzer, eine nicht
leere `club_id`, einen aktiven `TenantContext` und eine zum Club passende
Entität. Fehlt eine dieser Bedingungen, wird die Aktion verweigert. Es existiert
kein Laufzeit-Defaultclub. PlatformAdmins verwenden einen getrennten Scope.

Die Prüfung findet nicht nur im HTML statt, sondern in Authentifizierung,
Abhängigkeiten, `TenantSession`, Services, Jobs, Scheduler, Downloads,
Storage-Namespaces und Idempotency-/Cache-Schlüsseln. Direkte IDs eines anderen
Clubs liefern keinen nutzbaren Datensatz.

Bei `suspended`, `cancelled` oder `archived` bleiben berechtigte Lesezugriffe
erhalten, während mutierende Dashboardaktionen zentral abgewiesen werden.
Worker und Scheduler prüfen den Clubstatus erneut unmittelbar vor kosten- oder
publikationsrelevanter Arbeit. `setup_pending` erlaubt nur Einrichtungsarbeiten;
Generierung und Veröffentlichung verlangen weiterhin `trial` oder `active`.

## Plattformkonten

Die Datenbank erzwingt die XOR-Regel zwischen `club_user`/`club_id` und
`platform_admin`/ohne `club_id`. PlatformAdmin-Routen liegen unter `/platform`,
prüfen serverseitig den Kontotyp und bleiben CSRF-geschützt. Sitzungsänderungen
werden über `auth_version` wirksam.

## Medien und Secrets

S3-Zugangsschlüssel verbleiben serverseitig. Browser erhalten nur kurzlebige,
operationseingeschränkte Upload-URLs für einen festgelegten Club-Objektschlüssel.
Der Abschluss prüft Größe, Content-Type, Signatur, Prüfsumme und Namespace.
Private Objekte sind nicht öffentlich. Publishing verwendet getrennte,
kurzlebige Objekte/URLs.

## Prompt-Schutz

Promptkörper und finale Prompts werden nie an Vereinsseiten, öffentliche APIs,
allgemeine Fehler, Club-Audit oder Browser-Datenattribute übertragen.
Generierungssnapshots enthalten Referenz-IDs, Versionen und SHA-256. Freitexte
werden längenbegrenzt, escaped, auf Steueranweisungen geprüft und als Datenblock
unterhalb unveränderlicher Sicherheitsregeln eingefügt.

Finale Provider-Inputs liegen getrennt in `ai_prompt_dispatches` und werden nur
über eine ausdrücklich PlatformAdmin-geschützte Route angezeigt. Clubseiten,
Club-APIs und Club-Exporte fragen diese Tabelle nicht ab. Fehlerprotokolle
speichern nur bereinigte Fehlerklassen; Prompttexte sind auch dort verboten.

## Audit

Plattform- und Club-Audit sind logisch getrennt. Promptänderungen protokollieren
nur ID, Version, Prüfsumme, Änderungsart und Beschreibung. Secrets, Token und
Prompttexte sind verboten. Cross-Tenant-, Rollen-, Quoten- und Uploadtests sind
Teil der automatisierten Suite.
