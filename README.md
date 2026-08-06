# SocialMediaAgent

Sicherheitsorientierte, selbst gehostete Anwendung für automatisch erzeugte,
**immer manuell freizugebende** Fußball-Instagram-Beiträge. Freigegebene und
fällige Beiträge können in einer getrennten Produktionsumgebung kontrolliert
vom Scheduler veröffentlicht werden. UI und Betriebsdokumentation sind
deutsch; externe FUSSBALL.DE-, OpenAI-, SMB- und Meta-Zugriffe bleiben
standardmäßig Fixture/Mock/Dry-Run.

## Enthalten
- FastAPI/Jinja2/HTMX-Dashboard, Session-Login, Argon2, CSRF, RBAC und Mannschafts-Scope
- SQLAlchemy-2-Modell, Alembic, PostgreSQL/SQLite, Optimistic Locking und Auditmodell
- austauschbarer FUSSBALL.DE-HTML-Provider ohne erfundene API
- persistente automatische FUSSBALL.DE-Synchronisation mit stabiler Ergebniserkennung
- sicherer lokaler bzw. host-gemounteter SMB-Speicher und einmalige Bildreservierung
- automatische Feed-/Multi-Story-Erzeugung (1080×1350/1080×1920), Fakten-only Textgenerator
- versionierte Freigaben, einzelne Publishing-Aufträge, Not-Aus, Idempotenz und unklare Plattformzustände
- offizieller Graph-API-Publisher plus Mock/Dry-Run; Live-Modus ist mehrfach opt-in
- Docker Compose mit Web, Worker, PostgreSQL, Nginx, Healthchecks, Volumes, Backup/Restore

## Lokal starten
Python 3.12 vorausgesetzt:
```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env                 # lokal DATABASE_URL auf sqlite:///./data/app.db setzen
alembic upgrade head
python scripts/seed.py
uvicorn app.main:app --reload
```
Dann `http://localhost:8000`, initial `admin@example.invalid` / `ChangeMe-Immediately!`; sofort ändern. Tests: `pytest`; Lint: `ruff check .`.

Docker:
```bash
mkdir -p secrets && openssl rand -base64 32 > secrets/db_password.txt
cp .env.example .env                 # DB-Passwort in DATABASE_URL passend setzen
PUBLISHER_MODE=dry-run GLOBAL_PUBLISH_ENABLED=false docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

## Administration
1. **Instagram-Seite:** internen Namen, Username und offizielle Konto-ID anlegen; Token ausschließlich als Docker Secret/Environment. Nach offizieller Verbindungsprüfung aktivieren. Mehrere Teams dürfen dieselbe Seite nutzen; der Beitrag snapshotet sein Ziel.
2. **Mannschaft:** Namen/Slug, plausible `https://www.fussball.de/...`-URL, aktive Seite, relativen Medienunterordner, vorhandene Vorlagen/Fonts, Farben und Zeitzone anlegen. Löschen ist Soft-Delete/Archivierung.
3. **Rechte:** Rolle und Mannschaftszuordnung sind getrennt. `all_teams=false` verlangt explizite `UserTeam`-Zeilen; direkte URLs und Services prüfen serverseitig.

### Vereinsbranding

Der tenant-sichere **Branding-Assistent** verwaltet Grunddesign,
Bildgestaltung, Textsprache, strukturierte Sponsorenangaben sowie Feed- und
Story-Vorgaben. Farben, Schriften, Mannschaftsschreibweisen und dynamische
Beispiele können unmittelbar geprüft werden, ohne geschützte
Plattform-Prompttexte an den Browser zu übertragen. Vorhandene Freitextwerte
bleiben abwärtskompatibel erhalten. Bedienung, Datenstruktur, Rückfallwerte,
Mandantenschutz und Grenzen der Live-Vorschau sind in
[`docs/BRANDING.md`](docs/BRANDING.md) dokumentiert.

### Benutzerrollen

- **Administrator:** Vollzugriff einschließlich Benutzerkonten, Rollen, Mannschaftsrechten und Systemeinstellungen.
- **Redakteur:** Darf Beiträge erstellen, bearbeiten, per KI neu erzeugen und ausdrücklich freigeben.
- **Autor:** Darf Beiträge erstellen, bearbeiten und per KI neu erzeugen. Die Freigabe muss anschließend durch einen Redakteur oder Administrator erfolgen.
- **Betrachter:** Darf Inhalte ausschließlich lesen.

Rollen und Mannschaftszuordnungen werden unabhängig voneinander geprüft. Ein
Redakteur oder Autor kann daher nur für die ihm ausdrücklich zugewiesenen
Mannschaften handeln. Neue Benutzer können auf der Anmeldeseite eine
Registrierung beantragen, bleiben aber bis zur ausdrücklichen Freigabe durch
einen Administrator gesperrt. Nur Administratoren dürfen Konten direkt anlegen oder Rollen
ändern; der letzte aktive Administrator kann nicht herabgestuft werden.
4. **Zeitregeln:** Feed als Minuten vor Anpfiff; Story-Regeln referenzieren Anpfiff, geplantes Ende, Ergebniserkennung, Freigabe oder Folgetag, mit Offset/fester Uhrzeit. Jede Regel erzeugt einen Job; Kollisionen werden nicht unbemerkt dupliziert.
5. **Medien:** Spielerbilder können in der Medienbibliothek als JPG, PNG oder WebP mehrfach hochgeladen oder aus einem read-only eingebundenen Team-Unterordner eingelesen werden. Dashboard-Uploads liegen getrennt im persistenten Upload-Volume; die externe Medienwurzel bleibt schreibgeschützt. Ein Bild wird atomar einem Spiel reserviert und darf in dessen Feed/Storys wiederverwendet werden. Ohne Bild entsteht eine neutrale Grafik mit Prüfhinweis.
6. **Workflow:** Worker synchronisiert Spiele, erzeugt Beiträge automatisch, rendert alle Dateien und Text. Freigeber prüft Version, Ziel und abgelaufene Zeiten. Jede relevante Änderung setzt offene Jobs auf erneute Freigabe.
7. **Fehler:** Transiente Fehler werden begrenzt wiederholt. Token-/Rechtefehler stoppen. Timeout/unklare Antwort wird `uncertain`; Status muss bei Meta geprüft werden, bevor jemand erneut startet.

### Manuell erstellte Beiträge

Unter **Beitrag manuell erstellen** können berechtigte Benutzer einen Feed,
ein Karussell aus 2 bis 10 geordneten Bildern oder eine Story als JPG, PNG oder
WebP hochladen. Die Ausgangsbilder müssen nicht bereits das Instagram-Format
besitzen: Eine lokale Canvas-Vorschau erlaubt pro Bild Zoom sowie horizontale
und vertikale Ausrichtung. Das Backend validiert den gewählten Bereich erneut
und erzeugt daraus versionierte PNGs mit 1080 × 1350 Pixel für Feed und
Karussell beziehungsweise 1080 × 1920 Pixel für Storys. Die unveränderten
Originaldateien bleiben privat erhalten. Die Karussell-Reihenfolge lässt sich vor dem Absenden mit den
Pfeiltasten festlegen; ein gemeinsamer Text und ein Veröffentlichungszeitpunkt
in der Mannschaftszeitzone gelten für das gesamte Karussell. Die Anwendung
erlaubt bei Feed und Karussell zusätzlich bis zu 20 positionsbezogene
Instagram-Kontomarkierungen pro Bild. Der Benutzername wird ohne führendes
`@` eingefroren; die Position wird als normierter X-/Y-Wert direkt in der
finalen Zuschnittvorschau gewählt und beim Erzeugen des jeweiligen
Meta-Mediencontainers als `user_tags` übergeben. Markierungen eines
Karussells bleiben beim Umsortieren am zugehörigen Bild. Storys unterstützen
in diesem Workflow keine positionsbezogenen Kontomarkierungen. Ob Instagram
eine Markierung annimmt, hängt zusätzlich von Existenz, Sichtbarkeit und den
Markierungseinstellungen des Zielkontos ab.

Die Anwendung
verändert die Motive nicht und legt genau einen unfreigegebenen
Veröffentlichungsauftrag an. Er durchläuft denselben versionsgebundenen Prüf-,
Freigabe- und Scheduler-Ablauf wie ein automatisch erzeugter Beitrag. Bei
Bild-Storys wird der Text nur intern dokumentiert, weil Instagram dafür keinen
separaten Caption-Parameter unterstützt.

## SMB
Auf Linux z. B. `/etc/fstab` mit einer nur für root lesbaren Credentials-Datei verwenden:
```text
//server/share /mnt/social-media-assets cifs credentials=/root/.smb-social,ro,nosuid,nodev,noexec,uid=10001,gid=10001,file_mode=0440,dir_mode=0550 0 0
```
`MEDIA_HOST_ROOT=/mnt/social-media-assets`, Containerziel `/app/external-media`. Die Anwendung speichert weder SMB-Benutzer noch Passwort. Test: `findmnt /mnt/social-media-assets` und `/health`.

## Meta-Verbindung
Nur offizielle Meta-/Instagram-Schnittstellen verwenden. Konto-ID, passende professionelle Kontoart, App Review/Berechtigungen, öffentlich abrufbare Medien-URL und gültige Tokens gemäß **aktuell offizieller** Dokumentation konfigurieren. Niemals Instagram-Passwörter speichern. Erst nach Staging-Test `PUBLISHER_MODE=live` und separat `GLOBAL_PUBLISH_ENABLED=true`; zusätzlich müssen Seite, Team, Beitrag und Job aktiv/freigegeben sein. Der Containerstatus wird vor `media_publish` geprüft; Plattformbestätigung ist zwingend.

## Produktion, Betrieb und Umzug
Proxmox: private VM, Compose Production Override und Zugriff vorzugsweise Tailscale/VPN; Proxy nur an Loopback. Cloud: Firewall nur 80/443, Nginx hinter Caddy/Traefik mit automatischem TLS oder diesen Proxy um TLS ergänzen. Niemals Uvicorn-Reload öffentlich exponieren. Updates: Backup, Image bauen, `alembic upgrade head`, Compose rolling restart, Healthcheck prüfen.

Backup: `scripts/backup.sh`; Restore in Wartungsmodus: `scripts/restore.sh BACKUP`. Enthalten sind DB, Uploads, Vorschauen/Generiertes, Vorlagen und Compose-Konfiguration – keine `.env`/Secrets. SMB-Originale separat am Fileserver sichern. Bei Umzug: Backup übertragen, SMB neu mounten, Secrets neu setzen, Restore, Domain/TLS umstellen und alle Verbindungen im Dry-Run testen.

Not-Aus: `system_settings.key='emergency_stop'`, `value={"enabled":true}` stoppt noch nicht begonnene Jobs. Laufende/unklare Vorgänge zuerst bei Meta abgleichen. Weitere Schalter existieren global, je Seite, Team, Beitrag und Job.

## KI-Prompts, Bildgenerierung und Offline-Fallback
Unter **KI-Promptvorlagen** verwalten ausschließlich PlatformAdmins Bild- und Textprompts mit geprüften Jinja-Platzhaltern. Eingebaute sowie gespeicherte Vorlagen lassen sich einsehen und nur als neue, zunächst inaktive Version bearbeiten. Mannschaftsregeln ordnen getrennte Feed-, Story- und Textprompts zu; einzelne Story-Regeln dürfen einen eigenen Story-Prompt wählen. Die Fixture-Vorschau ersetzt Platzhalter mit Beispieldaten, ruft aber keine externe API auf. Beiträge speichern nur sichere Referenzen, Versionen und Prüfsummen. Der exakt an den Anbieter versandte Prompt wird separat und ausschließlich im PlatformAdmin-Bereich protokolliert und kann dort nach Verein sowie Bild/Text gefiltert werden. Details: [`docs/AI_PROMPT_OBSERVABILITY.md`](docs/AI_PROMPT_OBSERVABILITY.md).

Mit `IMAGE_GENERATOR_MODE=openai` und `TEXT_GENERATOR_MODE=openai` werden Grafiken beziehungsweise Begleittexte über die OpenAI API erzeugt. Standardmodell für Bilder ist `gpt-image-2`. Der Bildauftrag erhält die verifizierten lokalen Referenzen in fester Reihenfolge: Spielerfoto, eigenes Mannschaftslogo und – sofern vorhanden – Gegnerlogo. Der versionierte Sicherheitspräfix weist das Modell an, den Spieler wiederzuerkennen, das eigene Logo deutlich und harmonisch sowie das Gegnerlogo kleiner, aber klar erkennbar in die Gesamtkomposition zu integrieren. Ohne Gegnerlogo wird ausschließlich der Gegnername als neutrale Typografie verlangt; ein eigenes Mannschaftslogo ist für die KI-Erzeugung verpflichtend. Die Ausgabe wird auf exakt 1080 × 1350 (Feed) beziehungsweise 1080 × 1920 (Story) normalisiert und technisch als PNG validiert.

Die Originaldateien bleiben unverändert und werden zusammen mit ID, Version, Prüfsumme, Referenzreihenfolge und Prompt-Policy im Beitrag eingefroren. Ein generatives Bildmodell kann die pixelgenaue Logo-Wiedergabe, exakte Schrift oder das vollständige Fehlen zusätzlicher Fantasieelemente dennoch nicht garantieren. Die Vorschau zeigt deshalb die verifizierten Originale zum direkten Vergleich; die manuelle visuelle Freigabe bleibt zwingend.

Logo-Uploads liegen im persistenten Volume unter `UPLOAD_ROOT=/app/data/uploads`. Das read-only SMB-Verzeichnis wird dabei nie beschrieben. Mannschaftslogos werden unter **Mannschaften**, Gegnerlogos über den Button am jeweiligen Spiel verwaltet. Eine Logoänderung überschreibt keine bestehende Grafik und entzieht offene Freigaben. Bei neuen KI-Kompositionen ist danach eine vollständige Bild-Neugenerierung erforderlich, weil die Logos Bestandteil des Modellauftrags sind. Nur bestehende Legacy-Beiträge mit separat gespeicherter KI-Grundgrafik behalten die lokale Compositor-Neuzusammensetzung. Details: [`docs/VERIFIED_LOGOS.md`](docs/VERIFIED_LOGOS.md).

Vor Modellwechseln oder Produktivtests die aktuelle offizielle [OpenAI-Anleitung zur Bildgenerierung](https://developers.openai.com/api/docs/guides/image-generation) und die [Modellseite von GPT Image 2](https://developers.openai.com/api/docs/models/gpt-image-2) erneut prüfen.

`IMAGE_GENERATOR_MODE=playwright` bleibt der standardmäßige, reproduzierbare Offline-Fallback. Dabei werden Feed und Story aus HTML/CSS mit Playwright/Chromium gerendert. Die eingebauten Vorlagen `default-feed` und `default-story` unterstützen Ankündigung und Ergebnis; aktive Datenbankvorlagen werden in ihrer neuesten Version gewählt und vollständig im Beitragssnapshot eingefroren. Für lokale Entwicklung muss ein von Playwright nutzbares Chromium installiert sein; das Docker-Image installiert es automatisch.

Der API-Key wird ausschließlich über das Docker-Secret `openai_api_key` bereitgestellt. Für einen kontrollierten KI-Test in Staging:

```text
TEXT_GENERATOR_MODE=openai
IMAGE_GENERATOR_MODE=openai
OPENAI_MODEL=gpt-5-mini
OPENAI_IMAGE_MODEL=gpt-image-2
OPENAI_IMAGE_QUALITY=medium
```

Nach Änderung der `.env.staging` Web und Worker neu erstellen. Meta/Instagram bleibt davon unabhängig im Dry-Run.

Zeit- und kostenintensive Text- und Bildgenerierungen werden als persistente
PostgreSQL-Aufträge im vorhandenen Worker verarbeitet. Die Webanfrage reiht nur
ein und zeigt unmittelbar den Auftragsstatus; Nginx benötigt dafür keinen
mehrminütigen Request-Timeout. Zustände, Idempotenz, Leases, Abbruch, Retry und
der Umgang mit unklaren API-Antworten sind in
[`docs/GENERATION_JOBS.md`](docs/GENERATION_JOBS.md) dokumentiert.

Der FUSSBALL.DE-Parser ist fixture-getestet, muss aber bei HTML-Änderungen angepasst werden. Reale OpenAI-/Meta-Aufrufe wurden nicht durchgeführt. Details und Zustände: [ARCHITECTURE.md](ARCHITECTURE.md).

## Lokaler End-to-End-Test (ohne externe Dienste)
1. `.env.example` nach `.env` kopieren, `PUBLISHER_MODE=dry-run`, `GLOBAL_PUBLISH_ENABLED=false`, `FUSSBALL_LIVE_TEST_ENABLED=false` beibehalten und ein zufälliges Session-Secret setzen.
2. Einen lokalen Medienbaum `data/external-media/erste/` mit freigegebenen JPG/PNG-Dateien anlegen.
3. Migrationen und Seed ausführen, Anwendung starten und anmelden.
4. Im Dashboard eine Instagram-Seite anlegen und über **Mock-Verbindung prüfen** verbinden. Publishing bleibt dabei deaktiviert.
5. Mannschaft mit dem relativen Ordner `erste` anlegen, Medien neu einlesen, Ankündigungs-/Ergebnisregeln sowie mindestens zwei Story-Zeitpunkte konfigurieren.
6. Ein Fixture-Spiel und einen automatischen Beitrag über die vorhandenen Services/Worker erzeugen. Im Dashboard Feed, Text, Story-Aufträge und abgelaufene Zeitpunkte prüfen.
7. Beitrag ausdrücklich freigeben. Für die Verarbeitung `GLOBAL_PUBLISH_ENABLED=true` nur in dieser lokalen Dry-Run-Sitzung setzen. `DryRunPublisher` erzeugt ausschließlich `dry-run:*`-IDs und sendet nichts an Meta.
8. Not-Aus aktivieren und verifizieren, dass ein weiterer Auftrag ohne Versuch blockiert wird. Danach Testdatenbank und erzeugte Medien löschen.

Automatisierter Browser-/Integrationslauf: `pytest tests/test_dashboard.py`. Er deckt Anmeldung, CSRF, Instagram-Seite, Mock-Verbindung, Mannschaft, Story-Regel, Benutzeranlage, Teamrechte und verweigerte Administratorseiten ab.

## Kontrollierter FUSSBALL.DE-Live-Strukturtest
Der Modus ist standardmäßig aus und verändert weder Spiele noch Beiträge. Nur nach bewusster Aktivierung mit `FUSSBALL_LIVE_TEST_ENABLED=true` kann `python scripts/fussball_live_test.py TEAM_ID` öffentliches HTML lesen. Jeder Abruf wird unverändert und mit SHA-256 unter `data/provider-snapshots/` gespeichert; Parsergebnis oder ein klarer Strukturänderungsfehler landet zusätzlich in `provider_snapshots`. Der Modus plant und veröffentlicht nichts. Abrufintervall und rechtliche/robots-bezogene Vorgaben sind vor Einsatz zu prüfen.

## Erster Proxmox-Test
Benötigt werden eine Linux-VM mit Docker Engine/Compose v2, DNS oder Tailscale, ein als read-only eingebundenes SMB-Verzeichnis, ausreichend beschreibbare Docker-Volumes, zufällige Session-/DB-Secrets und ein TLS-Terminierungspunkt. Zuerst Backupziel und Restore prüfen, dann `docker compose config`, Images bauen, Migration/Healthchecks abwarten und den obigen Ablauf vollständig mit Dry-Run durchführen. Meta/OpenAI bleiben deaktiviert. Der Web-/Worker-Entrypoint führt `alembic upgrade head` aus; PostgreSQL-Passwort wird ausschließlich aus Docker Secret gelesen.

## Produktionsnahes Proxmox-Staging
Die abgesicherte Staging-Konfiguration, der einmalige Migrationsprozess, Docker-Secrets, read-only SMB, Systemprüfung, idempotente Dry-Run-Generalprobe, Provider-Diagnose sowie Backup-/Restore-Probe sind in [`docs/STAGING.md`](docs/STAGING.md) beschrieben. Einstieg: `.env.staging.example` kopieren, ausschließlich zufällige Secret-Dateien anlegen, `docker-compose.yml` mit `docker-compose.staging.yml` starten und anschließend `scripts/staging-check.sh` sowie `scripts/staging-smoke-test.sh` ausführen. Das Staging-Override erzwingt Dry-Run und entfernt Meta-Tokens.

## Getrennter Instagram-Meta-Test
Echte, ausschließlich manuell bestätigte Instagram-Tests laufen in einer eigenen
Compose-Umgebung. Das normale Proxmox-Staging bleibt dauerhaft im harten
`dry-run`. Einrichtung, OAuth, öffentliche Kurzzeit-Medienfreigaben,
`validate-only`, Container-Test, erster Feed-/Story-Test, Tokenpflege,
Not-Aus und unklare Plattformantworten sind in
[`docs/META_TEST.md`](docs/META_TEST.md) dokumentiert.

## Kontrollierte automatische Veröffentlichung

Die Scheduler-Automatik ist ausschließlich in der getrennten
`docker-compose.production.yml`-Umgebung verfügbar und beginnt deaktiviert.
Staging bleibt Dry-Run, der Meta-Test bleibt ausschließlich manuell. Umgebung,
drei gemeinsam zu aktivierende globale Gates, explizite Freigabe je
Instagram-Seite, Beitrags- und Versionsfreigabe, Not-Aus, Verbindung, Token,
Spielstatus, Zeitpunkt, Datei und Prüfsumme werden vor jedem externen Schritt
erneut geprüft. Container- und Media-IDs werden persistent gespeichert;
unklare schreibende Meta-Antworten werden nie automatisch wiederholt.

Einrichtung, Aktivierung, Pause, Not-Aus und Diagnose sind in
[`docs/AUTOMATIC_PUBLISHING.md`](docs/AUTOMATIC_PUBLISHING.md) beschrieben.

## FUSSBALL.DE-Mannschaftsspielplan
Der Provider unterstützt neben dem kompakten Testformat die öffentliche Tabelle `#id-team-matchplan-table`: Eine `.row-competition` liefert Datum, Berliner Uhrzeit, Wettbewerb und Spielnummer für die unmittelbar folgende Spielzeile; Heim/Gast und die stabile externe ID stammen aus `.column-club` beziehungsweise `/spiel/.../spiel/ID`. Für jeden erkannten Termin wird die öffentliche Spiel-Detailseite kontrolliert und mit begrenzter Abrufzahl gelesen. Daraus übernimmt die Anwendung Platzname, Platzart und Anschrift; schlägt eine Detailseite fehl, bleibt der Spielplaneintrag erhalten und wird mit einer Warnung versehen.

Ein `.hint-pre-publish` markiert Treffer als `provisional`. Standardmäßig bleibt deren Automatisierung gesperrt. Administratoren können dies je Mannschaft unter **Regeln & Storys** ausdrücklich erlauben; der originale Providerstatus bleibt trotzdem sichtbar und nachvollziehbar. Absagen und Verlegungen haben immer Vorrang und bleiben unabhängig von dieser Einstellung gesperrt. Nach einer Regeländerung wird ein aktivierter automatischer Abruf sofort fällig.

Ergebnisse aus normalen ASCII-Ziffern werden direkt gelesen. Dynamisch zugeordnete Ziffern werden nur über den streng validierten offiziellen FUSSBALL.DE-Font deterministisch aufgelöst; OCR und visuelles Raten sind ausgeschlossen. Ein Ergebnis wird erst nach zwei verschiedenen, zeitlich stabilen Snapshots automatisch bestätigt. Automatische Abrufe und Beitragserzeugung besitzen getrennte globale und mannschaftsbezogene Opt-ins. Beiträge bleiben standardmäßig manuell freizugeben; eine automatische Freigabe kann je Mannschaft und Beitragstyp ausdrücklich aktiviert werden und nutzt weiterhin sämtliche bestehenden Freigabeprüfungen. Betrieb und Diagnose beschreibt [`docs/AUTOMATIC_FUSSBALL.md`](docs/AUTOMATIC_FUSSBALL.md), die mannschaftsbezogene Zeit- und Freigabeplanung [`docs/FUSSBALL_SCHEDULING.md`](docs/FUSSBALL_SCHEDULING.md).

Die Provider-Diagnose bleibt read-only. Nach der Vorschau kann ausschließlich ein Administrator mit CSRF-Schutz und der Bestätigung `SPIELE ÜBERNEHMEN` Spiele idempotent importieren. Der Import erzeugt keine Beiträge. Öffentliche AJAX-Aufrufe sind technisch auf HTTPS, `fussball.de`/`www.fussball.de`, die drei bekannten `ajax.team.*`-Pfade, Größenlimit, Timeout und begrenztes Backoff beschränkt. Ob `ajax.team.prev.games` lesbare Ergebnisse liefert, wurde in dieser Änderung nicht live geprüft; verschleierte Werte bleiben deshalb leer.

## Mandantenfähige SaaS-Plattform

Die Anwendung besitzt ein explizites `Club`-Mandantenmodell. Normale Konten
gehören genau einem Verein; `PlatformAdmin`-Konten besitzen ausdrücklich keine
Vereinszuordnung. `TenantSession`, `TenantContext` und tenantgebundene Services
verweigern Zugriffe ohne eindeutigen Kontext. Die neue Migration übernimmt eine
bestehende Installation nur nach erfolgreicher Vorprüfung in einen explizit
konfigurierten initialen Verein.

Der getrennte Bereich `/platform` verwaltet Vereine, Vereinsadministratoren,
Tarif-/Limitprofile, Zusatzkontingente, Feature Flags, zentrale Prompts,
geschützte Vereinsanpassungen, Plattformtests, aggregierten Verbrauch und
Plattform-Audit. Selbstregistrierung und Zahlungsabwicklung bleiben über Feature
Flags deaktiviert.

Für neue SaaS-Uploads steht privater S3-kompatibler Objektspeicher mit
Club-UUID-Namespaces, direkten signierten Uploads, Abschlussvalidierung,
Storage-Reservierungen und Ledger zur Verfügung. Cloudflare R2, Hetzner Object
Storage, generisches S3 und lokaler Entwicklungsspeicher verwenden dieselbe
Schnittstelle. Der PlatformAdmin kann Datenbank und privaten Objektspeicher
vereinsweise oder plattformweit rein lesend abgleichen; automatische
Korrekturen oder Löschungen erfolgen dabei nicht. Die vollständige Betriebs-
und Rollout-Dokumentation beginnt bei
[`docs/SAAS_ADMINISTRATION.md`](docs/SAAS_ADMINISTRATION.md).
