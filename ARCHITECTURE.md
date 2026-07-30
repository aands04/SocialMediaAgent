# Architektur

## Zielbild und Komponenten
Der Social Media Agent ist ein **modularer Monolith**. `app/main.py` liefert FastAPI/Jinja2/HTMX, `app/models.py` und Alembic bilden PostgreSQL ab. Fachmodule kapseln Authentifizierung, Mannschaften, Instagram-Seiten, Spiele, Medien/SMB, Designs, Rendering, Text, Beiträge, Freigaben, Publishing, Jobs, Audit und Monitoring. Web und Worker sind getrennte Prozesse desselben Artefakts; PostgreSQL ist die einzige Wahrheitsquelle.

Externe Systeme liegen hinter Ports: `GameDataProvider`/`FussballDeProvider`, `StorageProvider`/`LocalStorageProvider`/`SmbStorageProvider`, `TextGenerator`, `ImageProvider` und `SocialMediaPublisher`. Standardmäßig werden Fixture-Texte, der lokale Playwright-Renderer und `DryRunPublisher` eingesetzt. Optional erzeugen `OpenAITextGenerator` und `OpenAIImageProvider` Begleittext und eigenständige Grafiken; beide bleiben von der Instagram-Veröffentlichung getrennt.

## Datenfluss
1. Der Worker liest öffentliches HTML über den gekapselten Provider und upsertet anhand `(team_id, provider, external_id)` Spiele.
2. Regeln berechnen UTC-Zeitpunkte. `create_post` reserviert transaktional das größte verfügbare Spielerbild, löst die jüngste aktive Promptversion auf, snapshotet Prompt/Design/Seite, erzeugt Text, Feed und alle Storys und legt pro Ausgabe einen `PublicationJob` an.
3. Vollständige Beiträge gelangen in `pending_approval`; fehlendes Bild oder Widersprüche führen zu `incomplete`/manueller Prüfung.
4. Ein berechtigter Benutzer genehmigt eine konkrete Beitragsversion und ausgewählte Aufträge. Eigene Bearbeitung und Freigabe ist zulässig.
5. Der Worker sperrt den Auftrag, prüft unmittelbar sämtliche Gates und ruft erst dann den Publisher. Nur bestätigte Antworten werden `published`.

## Datenmodell und Invarianten
`User`, `UserTeam`, `Team`, `InstagramPage`, `Game`, `MediaAsset`, `LogoAsset`, `StoryRule`, `PromptTemplate`, `Post`, `PublicationJob`, `AuditLog`, `Notification` und `SystemSetting` bilden das Kernmodell. Eindeutige Constraints verhindern doppelte Spiele, Hauptbeiträge, Story-Regeln, Promptversionen, Medienpfade, Logo-Prüfsummen und Idempotency Keys. Das einmalige `reserved_game_id` erlaubt dasselbe Bild für Feed und Story eines Spiels, nicht für andere Spiele. Beiträge speichern Seite, Design-, Prompt-, Farb-, Font-, Logo-, Medien- und Textversionen als Snapshot.

## KI-Generierung und Promptinvarianten
Bild- und Textprompts werden durch eine `SandboxedEnvironment` mit `StrictUndefined` gerendert. Nur explizit zugelassene Faktenplatzhalter sind erlaubt. Unveränderliche Sicherheitspräfixe verbieten erfundene Spielinformationen, Fantasielogos und zusätzliche Personen. Der Spielort wird vor dem Modellaufruf deterministisch normalisiert: Heimspiel `Habichtswaldstadion Ehlen`, Auswärtsrasen `RP [Ort]`, Auswärtskunstrasen `KR [Ort]`; eine fehlende Platzart blockiert den KI-Aufruf.

Im KI-Bildmodus wird nur das kanonisierte Spielerbild als generative Referenz verwendet. `gpt-image-2` erzeugt eine separat gespeicherte Grundgrafik. Danach prüft ein deterministischer Compositor die eingefrorenen Logo-IDs, Versionen und Prüfsummen und bettet ausschließlich verifizierte Originale ein; fehlt das Gegnerlogo, wird der Gegnername neutral gesetzt. Grund- und Finalgrafik bleiben getrennt versioniert. Eine reine Logo-Neuzusammensetzung verwendet die Grundgrafik erneut und ruft keinen Bildprovider auf. Modellbedingte Fantasiewappen außerhalb der geschützten Logobereiche können nicht zuverlässig automatisch erkannt werden; Snapshot und Dashboard kennzeichnen deshalb jede KI-Ausgabe zur manuellen Prüfung. `IMAGE_GENERATOR_MODE=playwright` ist der reproduzierbare Offline-Fallback.

Zeitpunkte sind timezone-aware UTC; Anzeige und Regelkonfiguration erfolgen in `Europe/Berlin`. Relative, unveröffentlichte Aufträge werden bei Verlegung verschoben. Absolute Zeitpunkte bleiben unverändert und werden als veraltet markiert. Bereits veröffentlichte Jobs bleiben unverändert.

## Zustandsmodelle
Beiträge: `detected → planned → creating → pending_approval → approved/scheduled → partially_published → published`; Nebenpfade sind `incomplete`, `rejected`, `reapproval_required`, `publishing_error`, `cancelled`.

Aufträge: `draft/unapproved → approved → scheduled → waiting → publishing → published`; Fehler führen begrenzt zu `retry_scheduled`, danach `failed`. Timeouts und unklare Antworten führen zu `uncertain` und **nie** zu blindem Retry.

## Jobs, Freigabe und Veröffentlichung
Worker selektieren fällige Zeilen und verwenden Datenbanksperren (`FOR UPDATE`, bei Medien `SKIP LOCKED`). Jeder Job trägt einen stabilen Idempotency Key. Exponentielles Backoff wird aus Versuchszahl und konfigurierter Basis berechnet; Token-/Berechtigungsfehler sind dauerhaft. Vor Publishing werden Freigabe und Versionsbindung, Seite, alle Not-Aus-Ebenen, Zeitpunkt, Datei, Idempotenz und Spieländerungen geprüft. Eine Inhaltsänderung entzieht allen offenen Jobs die Freigabe. Feed und Story sind unabhängig, weshalb Teilerfolg sichtbar bleibt.

Der `InstagramPublisher` nutzt ausschließlich die offizielle Graph API: Mediencontainer, Statusabfrage, `media_publish`. Keine Passwörter, Login-Automation oder inoffiziellen Bibliotheken. Die technische Meta-Konfiguration ist absichtlich opt-in; Live-Publishing erfordert zusätzlich `GLOBAL_PUBLISH_ENABLED=true`, verbundene Seite und Freigabe.

## Sicherheit
Argon2-Passwort-Hashes, serverseitig signierte HttpOnly-Sessions, SameSite-Cookies, Produktions-`Secure`, CSRF-Token, 15-Minuten-Sperre nach fünf Fehlversuchen, Inaktivitätsablauf, keine Registrierung sowie rollen- und mannschaftsbezogene serverseitige Prüfungen bilden die Basis. Pfade werden kanonisiert; absolute Pfade, Traversal, ausbrechende Symlinks und fremde Dateitypen werden verworfen. SMB wird nur vom Host gemountet, Credentials gelangen weder in DB noch Quellcode. Secrets kommen aus Docker Secrets/Environment. Optimistische Versionsfelder verhindern Lost Updates. Sicherheits- und Freigabeaktionen werden auditiert. TOTP-2FA kann am User-Modul ergänzt werden.

## Erweiterungspunkte
Neue Spielprovider, Mount-/Objektspeicher, Bildprovider, Browser-Renderer, Benachrichtigungskanäle, Publisher und Textmodelle implementieren die jeweiligen Ports. Scheduler und Publisher dürfen später durch PostgreSQL-backed APScheduler bzw. eine Queue ersetzt werden, ohne Fachmodelle oder Weboberfläche aufzuteilen.

## Externe Schnittstellenprüfung
Ein Abruf der offiziellen Meta-Dokumentation war am 27.07.2026 aus der isolierten Tool-Umgebung wegen HTTP 401 nicht möglich. Vor Live-Aktivierung müssen Betreiber die aktuelle offizielle Meta-Dokumentation zu Content Publishing, Stories, erforderlichen Berechtigungen, App Review, Kontoart, Tokenlaufzeiten und der verwendeten Graph-Version erneut prüfen. Die App behauptet daher keine fest verdrahtete, dauerhaft gültige Berechtigungsliste und bleibt standardmäßig im Dry-Run.

## Bedienbare Testversion
`admin_routes` stellt CSRF-geschützte, serverseitig autorisierte Workflows für Mannschaften, Seiten, Benutzer/Teamzuordnungen, SMB-Scan, Medienstatus, Fonts, versionierte Designs, Ankündigungs-/Ergebnisregeln, Multi-Story-Regeln, Beitragsprüfung/Freigabe/Ablehnung und Auftragsabbruch bereit. Versionsfelder verhindern unbemerkte parallele Statusänderungen. Verstrichene Jobs werden sichtbar markiert und gemäß Teamregel sofort, manuell, übersprungen oder am nächsten Story-Termin behandelt.

Der kontrollierte Live-Diagnosemodus speichert öffentlich abgerufenes FUSSBALL.DE-HTML unverändert mit Prüfsumme und Parserdiagnose. Er ist standardmäßig deaktiviert und besitzt keinerlei Schreibpfad zu Spielen, Beiträgen oder Veröffentlichungen.
