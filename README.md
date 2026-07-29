# SocialMediaAgent

Sicherheitsorientiertes, selbst gehostetes MVP für automatisch erzeugte, **immer manuell freizugebende** Fußball-Instagram-Beiträge. UI und Betriebsdokumentation sind deutsch; externe FUSSBALL.DE-, OpenAI-, SMB- und Meta-Zugriffe bleiben standardmäßig Fixture/Mock/Dry-Run.

## Enthalten
- FastAPI/Jinja2/HTMX-Dashboard, Session-Login, Argon2, CSRF, RBAC und Mannschafts-Scope
- SQLAlchemy-2-Modell, Alembic, PostgreSQL/SQLite, Optimistic Locking und Auditmodell
- austauschbarer FUSSBALL.DE-HTML-Provider ohne erfundene API
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
4. **Zeitregeln:** Feed als Minuten vor Anpfiff; Story-Regeln referenzieren Anpfiff, geplantes Ende, Ergebniserkennung, Freigabe oder Folgetag, mit Offset/fester Uhrzeit. Jede Regel erzeugt einen Job; Kollisionen werden nicht unbemerkt dupliziert.
5. **Medien:** SMB-Share auf dem Host mounten (siehe unten), Team-Unterordner scannen. Ein Bild wird atomar einem Spiel reserviert und darf in dessen Feed/Storys wiederverwendet werden. Ohne Bild entsteht eine neutrale Grafik mit Prüfhinweis.
6. **Workflow:** Worker synchronisiert Spiele, erzeugt Beiträge automatisch, rendert alle Dateien und Text. Freigeber prüft Version, Ziel und abgelaufene Zeiten. Jede relevante Änderung setzt offene Jobs auf erneute Freigabe.
7. **Fehler:** Transiente Fehler werden begrenzt wiederholt. Token-/Rechtefehler stoppen. Timeout/unklare Antwort wird `uncertain`; Status muss bei Meta geprüft werden, bevor jemand erneut startet.

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

## Design-Renderer und Grenzen des MVP
Feed (1080 × 1350) und Story (1080 × 1920) werden reproduzierbar aus HTML/CSS mit Playwright/Chromium gerendert. Die eingebauten Vorlagen `default-feed` und `default-story` unterstützen Ankündigung und Ergebnis; aktive Datenbankvorlagen werden in ihrer neuesten Version gewählt und vollständig im Beitragssnapshot eingefroren. Reservierte Originalbilder, Logos und lokal hochgeladene Fonts werden als Data-URLs eingebettet, sodass beim Rendern kein externer Abruf erfolgt. Fehlende Logos, Orte oder Fonts verwenden sichtbare, definierte Fallbacks. Für lokale Entwicklung muss ein von Playwright nutzbares Chromium installiert sein; das Docker-Image installiert es automatisch.

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

## FUSSBALL.DE-Mannschaftsspielplan
Der Provider unterstützt neben dem kompakten Testformat die öffentliche Tabelle `#id-team-matchplan-table`: Eine `.row-competition` liefert Datum, Berliner Uhrzeit, Wettbewerb und Spielnummer für die unmittelbar folgende Spielzeile; Heim/Gast und die stabile externe ID stammen aus `.column-club` beziehungsweise `/spiel/.../spiel/ID`. Ein `.hint-pre-publish` markiert sämtliche Treffer als `provisional` und sperrt deren Beitragserstellung. Private Symbolschrift-Glyphen mit `data-obfuscation` werden ausdrücklich nicht dekodiert; nur normale ASCII-Ziffern im vollständigen Format `Zahl : Zahl` werden als unbestätigtes Ergebnis gelesen.

Die Provider-Diagnose bleibt read-only. Nach der Vorschau kann ausschließlich ein Administrator mit CSRF-Schutz und der Bestätigung `SPIELE ÜBERNEHMEN` Spiele idempotent importieren. Der Import erzeugt keine Beiträge. Öffentliche AJAX-Aufrufe sind technisch auf HTTPS, `fussball.de`/`www.fussball.de`, die drei bekannten `ajax.team.*`-Pfade, Größenlimit, Timeout und begrenztes Backoff beschränkt. Ob `ajax.team.prev.games` lesbare Ergebnisse liefert, wurde in dieser Änderung nicht live geprüft; verschleierte Werte bleiben deshalb leer.
