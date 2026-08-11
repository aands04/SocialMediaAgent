# Architektur

## Zielbild und Komponenten
Der Social Media Agent ist ein **modularer Monolith**. `app/main.py` liefert FastAPI/Jinja2/HTMX, `app/models.py` und Alembic bilden PostgreSQL ab. Fachmodule kapseln Authentifizierung, Mannschaften, Social-Media-Kanäle, Spiele, Medien/SMB, Designs, Rendering, Text, Beiträge, Freigaben, Publishing, Jobs, Audit und Monitoring. Web und Worker sind getrennte Prozesse desselben Artefakts; PostgreSQL ist die einzige Wahrheitsquelle.

Externe Systeme liegen hinter Ports: `GameDataProvider`/`FussballDeProvider`, `StorageProvider`/`LocalStorageProvider`/`SmbStorageProvider`, `TextGenerator`, `ImageProvider` und `SocialMediaPublisher`. Standardmäßig werden Fixture-Texte, der lokale Playwright-Renderer und `DryRunPublisher` eingesetzt. Optional erzeugen `OpenAITextGenerator` und `OpenAIImageProvider` Begleittext und eigenständige Grafiken; beide bleiben von der Instagram-Veröffentlichung getrennt.

## Datenfluss
1. Der Worker liest öffentliches HTML über den gekapselten Provider und upsertet anhand `(team_id, provider, external_id)` Spiele.
2. Regeln berechnen UTC-Zeitpunkte. `create_post` reserviert transaktional das größte verfügbare Spielerbild, löst die jüngste aktive Promptversion auf, snapshotet Prompt/Design/Seite, erzeugt Text, Feed und alle Storys und legt pro Ausgabe einen `PublicationJob` an.
3. Vollständige Beiträge gelangen in `pending_approval`; fehlendes Bild oder Widersprüche führen zu `incomplete`/manueller Prüfung.
4. Ein berechtigter Benutzer genehmigt eine konkrete Beitragsversion und ausgewählte Aufträge. Eigene Bearbeitung und Freigabe ist zulässig.
5. Der Worker sperrt den Auftrag, prüft unmittelbar sämtliche Gates und ruft erst dann den Publisher. Nur bestätigte Antworten werden `published`.

## Datenmodell und Invarianten
`User`, `UserTeam`, `Team`, `InstagramPage`, `SocialChannelConnection`, `TeamChannelAssignment`, `Game`, `MediaAsset`, `LogoAsset`, `StoryRule`, `PromptTemplate`, `Post`, `PublicationJob`, `AuditLog`, `Notification` und `SystemSetting` bilden das Kernmodell. Eindeutige und zusammengesetzte Constraints verhindern doppelte Spiele, fremde Kanalzuordnungen, Hauptbeiträge, Story-Regeln, Promptversionen, Medienpfade, Logo-Prüfsummen und Idempotency Keys. WhatsApp-Empfänger, Vorlagen, Webhookereignisse und Auslieferungsversuche sind eigenständig und mandantengebunden. Das einmalige `reserved_game_id` erlaubt dasselbe Bild für Feed und Story eines Spiels, nicht für andere Spiele. Beiträge speichern Seite, Design-, Prompt-, Farb-, Font-, Logo-, Medien- und Textversionen als Snapshot.

## KI-Generierung und Promptinvarianten
Bild- und Textprompts werden durch eine `SandboxedEnvironment` mit `StrictUndefined` gerendert. Nur explizit zugelassene Faktenplatzhalter sind erlaubt. Unveränderliche Sicherheitspräfixe verbieten erfundene Spielinformationen, Fantasielogos und zusätzliche Personen. Der Spielort wird vor dem Modellaufruf deterministisch normalisiert: Bei Heimspielen gilt zuerst die im Vereinsbranding hinterlegte Kurzbezeichnung, danach die ausgewählte Standard-Heimspielstätte und erst danach der Spielort des Providers. Auswärtsrasen wird als `RP [Ort]`, Auswärtskunstrasen als `KR [Ort]` ausgegeben; eine fehlende Platzart blockiert den KI-Aufruf.

Im KI-Bildmodus werden lokale Referenzen in fester semantischer Reihenfolge übergeben: Spielerfoto, verifiziertes eigenes Mannschaftslogo, optional das verifizierte Gegnerlogo und danach alle für die konkrete Ausgabe geltenden Sponsorenlogos. Der versionierte Prompt weist `gpt-image-2` an, die Logos als Bestandteil der Sportgrafik einzusetzen, ohne ihre Form, Farben, Schriftzüge oder Emblembestandteile umzudeuten. Die Platzierungsangaben sind ungefähre Kompositionspräferenzen; es existieren keine festen Koordinaten oder reservierten Logoflächen. Fehlt das Gegnerlogo, verlangt der Prompt ausschließlich einen neutralen typografischen Gegnernamen. Die Referenz-IDs, Versionen, Prüfsummen, Reihenfolge und Prompt-Policy werden im Design-Snapshot eingefroren.

Ein serverseitiger Branding-Compiler übersetzt die validierte `ClubBrandingConfiguration` in semantische Bild- und Textregeln. Rohe Branding-JSON-Daten werden nicht an den Anbieter gesendet. Aktuelle Vereinsregeln haben bei stilistischen Konflikten Vorrang vor allgemeineren Vorlagenangaben; Fakten- und Sicherheitsregeln bleiben unveränderlich. Bildvarianten erhalten unterschiedliche Kompositionsrichtungen, gemeinsame Spieltage einen einzigen Textprompt mit den vollständigen Fakten aller Spiele.

Da die Logos bei neuen Ausgaben Teil der KI-Komposition sind, setzt der lokale Compositor keine Eck-Overlays mehr auf neue Grafiken. Nach einer Logoänderung ist eine vollständige, kostenpflichtige Neugenerierung erforderlich. Die alte Compositor-Pipeline bleibt ausschließlich für Legacy-Beiträge mit separat gespeicherter KI-Grundgrafik erhalten. Modellbedingte Logoabweichungen, fehlerhafte Schrift oder zusätzliche Fantasiewappen können nicht zuverlässig automatisch erkannt werden; Snapshot und Dashboard kennzeichnen deshalb jede KI-Ausgabe zur manuellen Prüfung. `IMAGE_GENERATOR_MODE=playwright` ist der reproduzierbare Offline-Fallback.

Zeitpunkte sind timezone-aware UTC; Anzeige und Regelkonfiguration erfolgen in `Europe/Berlin`. Relative, unveröffentlichte Aufträge werden bei Verlegung verschoben. Absolute Zeitpunkte bleiben unverändert und werden als veraltet markiert. Bereits veröffentlichte Jobs bleiben unverändert.

## Zustandsmodelle
Beiträge: `detected → planned → creating → pending_approval → approved/scheduled → partially_published → published`; Nebenpfade sind `incomplete`, `rejected`, `reapproval_required`, `publishing_error`, `cancelled`.

Aufträge: `draft/unapproved → approved → scheduled → waiting → publishing → published`; Fehler führen begrenzt zu `retry_scheduled`, danach `failed`. Timeouts und unklare Antworten führen zu `uncertain` und **nie** zu blindem Retry.

## Jobs, Freigabe und Veröffentlichung
Worker selektieren fällige Zeilen und verwenden Datenbanksperren (`FOR UPDATE`, bei Medien `SKIP LOCKED`). Jeder Job trägt einen stabilen Idempotency Key. Exponentielles Backoff wird aus Versuchszahl und konfigurierter Basis berechnet; Token-/Berechtigungsfehler sind dauerhaft. Vor Publishing werden Freigabe und Versionsbindung, Seite, alle Not-Aus-Ebenen, Zeitpunkt, Datei, Idempotenz und Spieländerungen geprüft. Eine Inhaltsänderung entzieht allen offenen Jobs die Freigabe. Feed und Story sind unabhängig, weshalb Teilerfolg sichtbar bleibt.

Das bestehende Staging verwendet ausschließlich `DryRunPublisher`. Der
Meta-Test kapselt den offiziellen Instagram-Login-Flow in `app/meta`: pro
Instagram-Seite verschlüsselte Verbindung, einmaliger OAuth-State, kurzlebige
öffentliche Medienfreigabe sowie persistenter Container- und
Publishing-Versuch. Containererstellung, Statusabfrage und `media_publish` sind
getrennte, manuell bestätigte Schritte. Gespeicherte Container- und Media-IDs
verhindern blinde Wiederholungen; unklare externe Antworten wechseln auf
`uncertain` und erfordern manuellen Abgleich. Scheduler und normaler
Publishing-Worker dürfen im Meta-Test keine echten Aufträge beanspruchen.

Die getrennte Produktionsumgebung kann ausdrücklich freigegebene und fällige
Aufträge automatisch verarbeiten. Der Worker startet diese Funktion nur, wenn
Produktionsmodus und drei globale Automatik-Gates gemeinsam aktiv sind. Jede
Instagram-Seite benötigt zusätzlich eine versioniert auditierte Freigabe. Vor
jedem externen Schritt werden Not-Aus, Verbindung, Berechtigungen, Token,
Seiten- und Medienartfreigabe, Beitragsversion, Spielstatus, Zeitpunkt, Datei
und Prüfsumme erneut geprüft. `MetaPublishingAttempt.trigger_mode` trennt
manuelle und automatische Abläufe. `next_action_at` macht die Statusabfrage
neustartsicher. Pro Durchlauf wird höchstens ein externer Schritt ausgeführt;
gespeicherte Container- und Media-IDs verhindern Doppelveröffentlichungen.
Unterbrochene möglicherweise schreibende Aufrufe werden `uncertain` und nie
automatisch wiederholt.

Das Modul `app/channels` ergänzt diesen bewährten Instagram-Ablauf um eine
gemeinsame Kanalabstraktion. Facebook ist ein Publishing-Kanal, WhatsApp ein
Nachrichtenkanal. Eine Freigabe erzeugt nur für die dabei ausdrücklich
ausgewählten und der Mannschaft zugeordneten Verbindungen neue Jobs. Die
kanalspezifischen Fähigkeiten verhindern Instagram-Story-Optionen für WhatsApp.
Page- und Cloud-API-Tokens sind verschlüsselt; Webhooks sind signiert,
idempotent und werden vor der Verarbeitung eindeutig einem Verein zugeordnet.
Details und aktuelle offizielle Meta-Voraussetzungen stehen in
[`docs/META_CHANNELS.md`](docs/META_CHANNELS.md).

## Sicherheit
Argon2-Passwort-Hashes, serverseitig signierte HttpOnly-Sessions, SameSite-Cookies, Produktions-`Secure`, CSRF-Token, 15-Minuten-Sperre nach fünf Fehlversuchen, Inaktivitätsablauf, keine Registrierung sowie rollen- und mannschaftsbezogene serverseitige Prüfungen bilden die Basis. Pfade werden kanonisiert; absolute Pfade, Traversal, ausbrechende Symlinks und fremde Dateitypen werden verworfen. SMB wird nur vom Host gemountet, Credentials gelangen weder in DB noch Quellcode. Secrets kommen aus Docker Secrets/Environment. Optimistische Versionsfelder verhindern Lost Updates. Sicherheits- und Freigabeaktionen werden auditiert. TOTP-2FA kann am User-Modul ergänzt werden.

## Erweiterungspunkte
Neue Spielprovider, Mount-/Objektspeicher, Bildprovider, Browser-Renderer, Benachrichtigungskanäle, Publisher und Textmodelle implementieren die jeweiligen Ports. Scheduler und Publisher dürfen später durch PostgreSQL-backed APScheduler bzw. eine Queue ersetzt werden, ohne Fachmodelle oder Weboberfläche aufzuteilen.

## Externe Schnittstellenprüfung
Die für die Meta-Testintegration verwendeten offiziellen Quellen, der
Abrufstand und die weiterhin vor jedem echten Test zu kontrollierenden
Voraussetzungen sind in [`docs/META_TEST.md`](docs/META_TEST.md) aufgeführt.
Die API-Version ist konfigurierbar und wird nicht als dauerhaft gültig
betrachtet. Tokenlaufzeiten werden aus den offiziellen API-Antworten
übernommen, nicht lokal erfunden. Die Produktionsautomatik wird deaktiviert
ausgeliefert und ist in
[`docs/AUTOMATIC_PUBLISHING.md`](docs/AUTOMATIC_PUBLISHING.md) beschrieben.

## Bedienbare Testversion
`admin_routes` stellt CSRF-geschützte, serverseitig autorisierte Workflows für Mannschaften, Seiten, Benutzer/Teamzuordnungen, SMB-Scan, Medienstatus, Fonts, versionierte Designs, Ankündigungs-/Ergebnisregeln, Multi-Story-Regeln, Beitragsprüfung/Freigabe/Ablehnung und Auftragsabbruch bereit. Versionsfelder verhindern unbemerkte parallele Statusänderungen. Verstrichene Jobs werden sichtbar markiert und gemäß Teamregel sofort, manuell, übersprungen oder am nächsten Story-Termin behandelt.

Der kontrollierte Live-Diagnosemodus speichert öffentlich abgerufenes FUSSBALL.DE-HTML unverändert mit Prüfsumme und Parserdiagnose. Er ist standardmäßig deaktiviert und besitzt keinerlei Schreibpfad zu Spielen, Beiträgen oder Veröffentlichungen.

## Mandanten- und Plattformgrenze

`Club.id` ist die unveränderliche Sicherheits- und Storage-Grenze. Slug, Name
oder Mannschaftsname sind niemals Berechtigungsmerkmale. Jede mandantenbezogene
Entität trägt eine direkte `club_id`; zusammengesetzte Constraints sichern
kritische Beziehungen zusätzlich ab. `TenantSession` ergänzt Loader-Kriterien,
prüft neue und geänderte Objekte vor dem Flush und verweigert nackte
mandantenbezogene Datenbankzugriffe ohne aktiven Scope. HTTP-Anfragen aktivieren
den Scope aus der authentifizierten Session, Worker aus der persistenten
`club_id` ihres Jobs. Plattformoperationen verwenden einen getrennten
`PlatformContext`.

Normale Benutzer sind `club_user` mit genau einer `club_id`. `platform_admin`
ist ein getrennter Kontotyp ohne `club_id`. Vereinsrollen und
Mannschaftszuweisungen wirken ausschließlich innerhalb dieses Vereins.
Statuswechsel auf `suspended` oder `archived` erhöhen die Sitzungs-/Clubversion,
blockieren neue mutierende Arbeiten und lassen vorhandene Inhalte lesbar.

## Quoten, Storage und Prompts

Effektive Limits entstehen nachvollziehbar aus versioniertem Tarifprofil,
Club-Override und zeitlich begrenztem Zusatzkontingent. Storage- und KI-Nutzung
werden vor der Arbeit atomar reserviert und anschließend idempotent committed
oder freigegeben. Ledger sind die Quelle der Verbrauchshistorie; Summen sind nur
abgeleitete Ansichten.

Neue SaaS-Objekte liegen privat unter
`clubs/{club_uuid}/{category}/{object_uuid}`. Direkte Uploads erhalten nur
kurzlebige signierte URLs und werden nach dem Upload serverseitig geprüft.
Bei der Mannschaftsanlage erzeugt der Server zusätzlich den lokalen
Kompatibilitätsnamespace
`clubs/{club_uuid}/teams/{team_uuid}-{technischer_slug}/` mit den Bereichen
`players`, `logos`, `backgrounds` und `imports`. Der Slug wird automatisch aus
dem internen Mannschaftsnamen abgeleitet und ist kein Berechtigungsmerkmal;
Vereins- und Mannschafts-UUID bleiben die Sicherheitsgrenze. Social-Media-
Verbindungen werden optional über mandantengebundene
`TeamChannelAssignment`-Zeilen zugewiesen, nicht über ein Pflichtfeld für
Instagram.
Bestehende lokale Dateipfade bleiben vorerst über den Legacypfad lesbar; SMB ist
nur Importprovider. Diese schrittweise Kompatibilität verhindert einen
ungeprüften Big-Bang-Umzug produktiver Medien.

Zentrale Prompttexte sind ausschließlich im PlatformAdmin-Kontext sichtbar.
Clubbenutzer bearbeiten validierte strukturierte Bild- und Textparameter.
Generierungssnapshots enthalten IDs, Versionen und Prüfsummen, aber keine
Prompttexte. PlatformAdmin-Fixturetests werden auditiert und als nicht
abrechenbare Plattformnutzung im Usage-Ledger erfasst.

`AiPromptDispatch` speichert den finalen Provider-Input unmittelbar vor einem
echten KI-Aufruf in einem separaten, tenantreferenzierten Plattformdatensatz.
Die Plattformroute `/platform/ai-generations` ist von Clubrouten getrennt und
erfordert ausdrücklich `PlatformAdmin`. Ein zusammengesetzter Idempotency Key
aus Job, Versuch, Prompt-Art, Medium und Aufrufindex verhindert doppelte
Dispatch-Einträge bei einer Wiederaufnahme. Club-Snapshots und Club-Exporte
enthalten weiterhin nur nicht geheime Metadaten.

Gemeinsame Spieltage besitzen keinen zweiten, parallelen Beitragstyp. Ein
persistenter `GenerationJob` koordiniert mehrere eingefrorene `Game`-IDs,
erzeugt die vorhandenen spielbezogenen `Post`- und Story-Objekte und übergibt
einen einmalig erzeugten gemeinsamen Text an alle Mitglieder. Erst danach
wandelt der bestehende Karussell-Koordinator den primären Feed-Auftrag in ein
Karussell um; die übrigen Feed-Aufträge werden nachvollziehbar als gebündelt
abgebrochen. Manuelle Gruppen liegen versioniert in `Game.overrides`.

Der globale `SharedOpponentLogo`-Katalog ist bewusst kein Tenant-Asset und
enthält eine eigene verifizierte Binärkopie. Die Auswahl importiert diese Datei
in ein neues, tenantgebundenes `LogoAsset`; dadurch bleiben alle Game-FKs,
Snapshots, Downloads und Storage-Prüfungen an der bisherigen Mandantengrenze.

Der Vereinsbereich stellt diese strukturierten Parameter über einen
fünfteiligen Branding-Assistenten bereit. `ClubBrandingConfiguration` bleibt
versionierte Quelle; eindeutig übertragbare Altwerte werden normalisiert,
unklare Werte unter `legacy_values` erhalten. Mannschaften, aus Heimspielen
abgeleitete Spielstätten, Logos, Medien und Schriftarten werden im aktiven
`TenantSession`-Scope geladen und beim Schreiben nochmals über ihre IDs
geprüft. Die browserseitige Vorschau erhält ausschließlich diese validierten
Brandingwerte und dynamische Bezeichnungen des aktuellen Vereins, jedoch keine
zentralen oder zusammengesetzten Prompttexte. Details stehen in
[`docs/BRANDING.md`](docs/BRANDING.md).

## Unveränderliche Medien- und Textversionen

Generierte Inhalte werden fachlich in Ausgabe und Version getrennt.
`GeneratedMediaSlot` bezeichnet eine konkrete Feed-/Karussellposition oder
Story-Ausgabe; `GeneratedMediaVersion` bewahrt jede technisch validierte Datei
mit Prüfsumme und Herkunft unveränderlich auf. `PostTextVersion` bietet dieselbe
Historie für Begleittexte. Die Auswahl eines Slots kann automatisch der neuesten
Version folgen oder manuell auf einer älteren Version stehen bleiben.

Eine Freigabe friert konkrete Text- und Medienversions-IDs in
`PublicationJob` beziehungsweise `PublicationMediaItem` ein. Neue
Generierungen ersetzen daher weder freigegebene noch veröffentlichte Dateien.
Die zentrale Session-Schicht verweigert skalare Änderungen historischer
Versionen; parallele Versionsanlage sperrt den Slot und vergibt fortlaufende
Nummern transaktionssicher.

`ContentRuleSet` trennt die Anzahl erzeugter Feed-/Story-Ausgaben von den
`PublicationRuleSlot`-Zeitpunkten. Die Hierarchie ist Spiel, Mannschaft,
Verein. Für einen nicht konfigurierten Spielwochentag wird bewusst keine
Ersatzzeit aus einem anderen Wochentag angenommen; die Ausgabe bleibt sichtbar
und wird als manuell zu planen markiert. Details und Migrationsstrategie stehen
in [`docs/MEDIA_VARIANTS_AND_RULES_PLAN.md`](docs/MEDIA_VARIANTS_AND_RULES_PLAN.md).

## Live-Ereignisse

`MatchEvent` ist die append-orientierte, mandantengebundene Quelle für
Spielphasen, Tore, Karten, Wechsel, Unterbrechungen und Korrekturen.
`LiveGameState` materialisiert ausschließlich bestätigte Ereignisse. Reporter,
Mannschaftszuordnungen, Regeln und Auslieferungsentscheidungen sind eigenständige
tenantgebundene Tabellen. Webhook-Eingänge aktivieren den Tenant-Scope erst nach
eindeutiger Zuordnung von WABA/Telefonnummer-ID; Provider-Nachrichten-ID und
Folgeentscheidung besitzen getrennte Idempotenzschlüssel. Externe
Auslieferungen umgehen weder die bestehenden Kanal- und Not-Aus-Schalter noch
Opt-in-, Template-, Freigabe- oder Quotenprüfungen. Details stehen in
[`docs/LIVE_CENTER.md`](docs/LIVE_CENTER.md).
