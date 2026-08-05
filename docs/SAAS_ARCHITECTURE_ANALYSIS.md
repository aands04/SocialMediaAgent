# Bestandsanalyse zur mandantenfähigen SaaS-Umstellung

Stand der Analyse: 5. August 2026
Ausgangsbasis: `origin/main` bei `57d7ab1`

## 1. Bestehende mandantenbezogene Modelle

Die Anwendung besitzt noch kein unveränderliches Mandantenobjekt. Der sichtbare
Vereinsname wird derzeit in `Team.club` und `InstagramPage.club` als freier Text
dupliziert. Die Mehrzahl der Fachdaten ist nur über `team_id` indirekt gruppiert.

Direkt an eine Mannschaft gebunden sind insbesondere Spiele, Medien,
Story-Regeln, Beiträge, Veröffentlichungsaufträge, Generierungsaufträge,
Provider-Snapshots, FUSSBALL.DE-Synchronisationszustände und Teile des Audits.
Instagram-Verbindungen hängen an einer Instagram-Seite. Globale Tabellen wie
`users`, `system_settings`, `font_assets`, `design_templates` und
`prompt_templates` besitzen keine Vereinszuordnung.

Wesentliche bestehende Eindeutigkeiten sind global statt mandantenbezogen:

- `users.email`
- `teams.slug`
- Pfade und Namen von Logos, Fonts und Provider-Snapshots
- mehrere Idempotency Keys
- Prompt- und Designvorlagennamen

Damit können identische Namen und externe Kennungen verschiedener Vereine noch
nicht sicher nebeneinander betrieben werden.

## 2. Bestehende Benutzer- und Berechtigungslogik

`User.role` kennt `admin`, `approver`, `editor` und `viewer`. `UserTeam`
beschränkt optional Mannschaften; `all_teams` hebt diese Einschränkung auf.
`allowed()` prüft Rolle und gegebenenfalls eine Mannschafts-ID. Eine
Vereinszuordnung, einen PlatformAdmin-Kontext oder eine Prüfung, ob die
Mannschaft zum Verein des Benutzers gehört, gibt es noch nicht.

Sessions enthalten Benutzer-ID und `auth_version`. Das ist eine gute Basis zum
Invalidieren von Sitzungen bei Vereinswechsel, Sperrung und Archivierung.
Login-Sperren, Passwort-Hashing, CSRF und rollenbezogene Aktionsrechte sind
bereits vorhanden und müssen erhalten bleiben.

Risiken:

- direkte IDs können ohne zentralen Tenant-Filter geladen werden;
- globale Adminrechte entsprechen derzeit faktisch Plattformrechten;
- Hintergrundjobs tragen keine direkte `club_id`;
- Teamzuweisungen können nicht per Constraint auf denselben Verein begrenzt
  werden;
- System- und Auditdaten sind nicht in Plattform- und Vereinsdaten getrennt.

## 3. Bestehende Speicherprovider

Die bestehende Abstraktion umfasst `LocalStorageProvider` und
`SmbStorageProvider`. Medien werden überwiegend als lokale absolute oder
relative Pfade gespeichert. Der primäre Upload-, Generierungs- und
Veröffentlichungsfluss erwartet lokal lesbare Dateien. Temporäre öffentliche
Meta-Medienfreigaben verweisen ebenfalls auf lokale Pfade.

Es fehlen:

- ein objektbasierter Providervertrag;
- private S3-/R2-/Hetzner-Namespaces;
- unveränderliche Vereins-UUID im Objektschlüssel;
- signierte direkte Uploads;
- Storage-Objektmetadaten und Storage-Ledger;
- Speicherreservierungen und Reconciliation;
- providerunabhängige temporäre Publishing-Objekte.

SMB eignet sich bereits als lesender Importpfad, darf für SaaS aber nicht
primärer Speicher bleiben.

## 4. Bestehende Promptverwaltung

`PromptTemplate` speichert versionierte Vorlagen, ist aber global sichtbar und
wird über normale Administrationsrouten verwaltet. `ResolvedPrompt.snapshot()`
speichert aktuell sowohl Vorlageninhalt als auch vollständig gerenderten Prompt
im `Post.design_snapshot`. `post_detail.html` gibt den gerenderten Prompt aus.
Das widerspricht dem geforderten Schutz der Plattform-Geschäftslogik.

Positive Grundlagen sind:

- Jinja-Sandbox und erlaubte Platzhalter;
- zentrale Fakten- und Sicherheitspräfixe;
- Versionierung aktiver Vorlagen;
- Modell- und Qualitäts-Snapshots.

Erforderlich sind ein ausschließlich plattformseitiges Promptmodell,
geschützte Vereinsanpassungen, strukturierte Branding-/Textparameter und ein
öffentlicher Snapshot ohne Klartext-Prompts.

## 5. Bestehende KI-Verbrauchslogik

Textantworten erfassen teilweise Tokenzahlen im Design-Snapshot.
Generierungsaufträge sind persistent, idempotent und lease-basiert. Ein
mandantenbezogenes Usage-Ledger, Abrechnungsperioden, Kontingentreservierungen
und die Unterscheidung technisch verwendbar/technisch fehlgeschlagen fehlen.

Vorhandene Jobstatus und Wiederanlaufmechanismen sind eine gute Basis. Die
Quotenreservierung muss jedoch vor dem ersten kostenpflichtigen Anbieteraufruf
in derselben Datenbank wie der Job erfolgen und über `club_id` sowie einen
Idempotency Key abgesichert werden.

## 6. Betroffene Migrationen

Die bestehende lineare Historie reicht von `0001` bis `0015`. Keine vorhandene
Migration wird verändert. Die SaaS-Erweiterung beginnt bei `0016`.

Die erste Migration muss in einer kontrollierten Reihenfolge:

1. Club-, Tarif- und Plattformtabellen anlegen;
2. einen initialen Verein erzeugen;
3. vorhandene Vereinsnamen vorab auf Eindeutigkeit prüfen;
4. `club_id` zunächst nullable ergänzen;
5. bestehende Daten deterministisch zuordnen;
6. Widersprüche erkennen und die Transaktion abbrechen;
7. Constraints und `NOT NULL` erst nach erfolgreichem Backfill setzen;
8. einen Migrationsbericht persistieren.

Besonders kritisch sind Tabellen ohne bisherigen Teambezug sowie historische
Audit- und Systemdaten. Für sie ist eine explizite Klassifizierung als
`platform` oder `club` nötig; es darf kein stillschweigender Standardverein in
der Laufzeit entstehen.

## 7. Risiken der Umstellung

### Hohe Risiken

- horizontale Rechteausweitung durch eine übersehene globale ID-Abfrage;
- inkonsistenter Backfill bei mehreren existierenden Vereinsnamen;
- doppelte oder fehlende Verbuchung bei Worker-Neustarts;
- lokale Pfade ohne Vereinsnamespace;
- Offenlegung bestehender Prompt-Snapshots;
- Race Conditions bei letzten Kontingenten und Limits;
- Scheduler- oder Publishing-Jobs, die nach Vereinsperrung weiterlaufen.

### Gegenmaßnahmen

- nicht optionaler `TenantContext` an allen Servicegrenzen;
- direkte `club_id` auf allen sicherheitsrelevanten Fachtabellen;
- zusammengesetzte Fremdschlüssel/Constraints, wo PostgreSQL dies sinnvoll
  erzwingen kann;
- zentrale tenantgebundene Ladefunktionen statt nacktem `db.get()`;
- deny-by-default bei fehlendem oder widersprüchlichem Kontext;
- PostgreSQL-Tests für Locks, Reservierungen und Parallelität;
- explizite Plattform- und Vereinskontexte;
- einmalige Datenmigration mit Vorprüfung und Bericht;
- neue Prompt-Snapshots ohne Prompttext;
- schrittweise Providerumstellung mit lokalem Kompatibilitätsprovider.

## 8. Nicht verhandelbare Invarianten

1. Ein Vereinsbenutzer hat genau eine `club_id`; ein PlatformAdmin keine.
2. Fehlender Tenant-Kontext führt zu einer Verweigerung, nie zu einem Default.
3. Jede kosten-, speicher- oder veröffentlichungsrelevante Aktion prüft Verein,
   Status, Berechtigung und effektives Limit serverseitig.
4. Jeder Hintergrundjob und jeder Idempotency-/Cache-Schlüssel enthält die
   unveränderliche Club-UUID.
5. Prompts werden ausschließlich serverseitig zusammengesetzt; Vereinsbenutzer
   erhalten keine Prompttexte oder rekonstruktionsfähigen Snapshots.
6. Private Medien liegen unter `clubs/{club_uuid}/...`; Slugs sind nie
   Sicherheitsgrenzen.
7. Sperren und Archivieren löschen keine Daten, blockieren aber neue mutierende
   oder externe Aktionen.
