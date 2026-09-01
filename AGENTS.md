# SocialMediaAgent – Arbeitsanweisungen für Codex

Diese Datei gilt für das gesamte Repository. Sie ergänzt `README.md`,
`ARCHITECTURE.md`, `OPERATIONS.md` und die thematischen Dokumente unter `docs/`.
Vor fachlichen Änderungen zuerst die einschlägige Dokumentation und die
betroffenen Tests lesen.

## Ziel und Architektur

SocialMediaAgent ist eine sicherheitsorientierte, selbst gehostete Anwendung
für Fußballvereine. Sie importiert Spielinformationen, erzeugt versionierte
Social-Media-Inhalte, führt sie durch Freigabe- und Zeitplanungsabläufe und kann
sie kontrolliert an Instagram, Facebook, WhatsApp oder FuPa übergeben.

- `app/main.py`: FastAPI-Anwendung, Jinja2/HTMX-Oberfläche und Router.
- `app/models.py`: kanonisches SQLAlchemy-Domänenmodell.
- `app/worker.py`: Generierung, FUSSBALL.DE-Sync, Publishing, Live Center,
  Creative Intelligence und FuPa-Spielbericht-Zyklen.
- `app/tenancy/`: verpflichtender Tenant-Kontext und Deny-by-default-Grenzen.
- `app/jobs/`, `app/posts/`, `app/publishing/`, `app/meta/`, `app/channels/`:
  persistente Generierungs-, Freigabe- und Veröffentlichungsabläufe.
- `app/games/`, `app/match_reports/`, `app/live/`: FUSSBALL.DE-, FuPa- und
  Live-Ereignisverarbeitung.
- `app/imagegen/`, `app/textgen/`, `app/rendering/`, `app/creative/`:
  KI-/Fallback-Generierung, Rendering und Creative Intelligence.
- `alembic/versions/`: versionierte Datenbankmigrationen; der auf einem
  Zielsystem tatsächlich angewendete Stand muss separat geprüft werden.
- `docker-compose.yml`: Basisdienste `db`, `migrate`, `web`, `worker`, `proxy`.
- `docker-compose.staging.yml`, `docker-compose.meta-test.yml` und
  `docker-compose.production.yml`: strikt getrennte Betriebsprofile.
- `deploy/nginx/`: interner Dashboard-Proxy und eng begrenzter öffentlicher
  Callback-/Webhook-/Kurzzeitmedien-Proxy.
- `tests/`: umfangreiche Service-, Route-, Sicherheits-, Tenant-, Publishing-
  und PostgreSQL-Tests.

PostgreSQL ist produktiv maßgeblich; SQLite dient den meisten lokalen Tests.
Web und Worker verwenden gemeinsame persistente Daten-/Medienverzeichnisse.
Der separate `migrate`-Dienst führt Alembic unter einem PostgreSQL Advisory
Lock aus, bevor Web und Worker starten.

## Nicht verhandelbare Invarianten

- Freigabe-, Versions-, Zielseiten-, Kanal-, Zeit-, Not-Aus- und globale Gates
  niemals umgehen oder abschwächen. Eine automatische Freigabe darf nur über
  die bereits vorhandenen, ausdrücklich aktivierten Regeln erfolgen.
- Einen möglicherweise von Meta angenommenen Schreibaufruf im Zustand
  `uncertain` niemals blind wiederholen. Zuerst anhand persistierter Container-
  und Media-IDs mit der Plattform abgleichen.
- Veröffentlichte oder freigegebene Text-/Medienversionen bleiben unveränderlich.
  Neue Varianten werden versioniert und entziehen bei relevanten Änderungen
  erforderlichenfalls die alte Freigabe.
- Jede mandantenbezogene Abfrage und Änderung benötigt den korrekten
  `tenant_scope`; fremde IDs müssen serverseitig erneut validiert werden.
- Externe HTML-/API-Daten nie durch geratene Fakten ergänzen. Parser und
  Browserautomation brechen bei unbekannten Strukturen sicher ab.
- SMB ist read-only Importquelle. Uploads und erzeugte Dateien gehören in die
  dafür vorgesehenen privaten, tenantgebundenen Speicherpfade.
- Zeiten intern in UTC speichern; in der Oberfläche `Europe/Berlin` verwenden.
- Echte OpenAI-, Meta-, Telegram-, FuPa- oder FUSSBALL.DE-Aufrufe nur nach
  ausdrücklicher Freigabe und mit den dokumentierten Gates. Tests bleiben
  standardmäßig Mock/Fixture/Dry-Run.
- Keine Secrets, Tokens, Cookies, Passwörter, vollständigen `.env`-Inhalte oder
  FuPa-Sitzungsdateien ausgeben, committen oder in Logs übernehmen.

## Git-Workflow und Remotes

Vor jeder Arbeit ausführen:

```powershell
git status --short --branch
git branch --show-current
git remote -v
```

Die Remote-Namen unterscheiden sich bewusst zwischen lokalem Checkout und VPS:

- Lokal ist `github` das kanonische GitHub-Repository
  `https://github.com/aands04/SocialMediaAgent.git`.
- Lokal zeigt `origin` per SSH auf den VPS-Checkout
  `/opt/socialmediaagent`. Diesen Remote nicht als normalen Push-Zielremote
  verwenden.
- Auf dem VPS zeigt `origin` auf GitHub.

Normaler Ablauf:

1. vom aktuellen `main` einen eng geschnittenen `feature/...`- oder `fix/...`-
   Branch erstellen;
2. Änderung und passende Tests gemeinsam implementieren;
3. lokal Lint, Tests und Diff prüfen;
4. Branch nach `github` pushen;
5. Pull Request nach `main`, Review und Merge;
6. erst nach ausdrücklichem Auftrag kontrolliert per SSH auf den VPS deployen.

Keine Commits, Pushes, PRs, Merges, Remote-Branch-Löschungen oder Deployments
ohne entsprechenden Benutzerauftrag. Vorhandene fremde Änderungen im Worktree
nicht überschreiben, aufräumen oder in den eigenen Commit aufnehmen.

## Lokale Einrichtung

Das Projekt verlangt Python 3.12 oder neuer. Bevorzugt eine eigene `.venv`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
alembic upgrade head
python scripts/seed.py
uvicorn app.main:app --reload
```

Für Linux/macOS stehen die entsprechenden Befehle im `README.md`. Lokale
Standardwerte müssen SQLite, Mock-/Fixture-Generatoren, `PUBLISHER_MODE=dry-run`
und `GLOBAL_PUBLISH_ENABLED=false` verwenden.

## Verifikation

Mindestens die zum geänderten Modul passenden Tests ausführen. Vor Übergabe
einer größeren Änderung nach Möglichkeit:

```powershell
python -m ruff check .
python -m ruff format --check .
python -m pytest -q
git diff --check
```

Wichtige gezielte Läufe:

```powershell
python -m pytest -q tests/test_worker_modes.py
python -m pytest -q tests/test_meta_integration.py
python -m pytest -q tests/test_social_channels.py
python -m pytest -q tests/test_automatic_fussball.py
python -m pytest -q tests/test_match_reports.py tests/test_telegram_match_feedback.py
python -m pytest -q tests/test_saas_tenancy_limits.py tests/test_saas_storage_prompts.py
```

PostgreSQL-spezifische Nebenläufigkeits- und Constraint-Tests benötigen eine
ausschließlich dafür bestimmte, wegwerfbare Datenbank:

```powershell
$env:TEST_POSTGRES_URL = "postgresql+psycopg://.../disposable_test_db"
python -m pytest -q -m postgresql tests/test_postgresql.py
```

Nie eine produktive oder anderweitig wertvolle Datenbank als
`TEST_POSTGRES_URL` verwenden. Docker-Profile vor einem Deployment nur mit
`config -q`/`config --quiet` prüfen, damit expandierte Umgebungswerte nicht in
Ausgaben landen.

Im Repository existiert derzeit keine GitHub-Actions-Konfiguration. Bis CI
hinzugefügt und verpflichtend gemacht wurde, sind lokal dokumentierte
Prüfergebnisse besonders wichtig.

## Regeln für Produktionsänderungen

- Eine Bestandsaufnahme oder Diagnose autorisiert ausschließlich lesende
  Prüfungen. Sie autorisiert weder Migration, Konfigurationsänderung, Neustart,
  Image-Build, Deployment noch das Abarbeiten oder Zurücksetzen von Jobs.
- Vor jeder produktiven Änderung Zielcommit, betroffene Dienste,
  Datenbankwirkung, erwartete Unterbrechung und sicheren Rückweg benennen.
- Zuerst ein aktuelles, lesbares und wiederherstellbares Backup nachweisen.
  Eine vorhandene Datei oder ein Zeitstempel allein ersetzt keinen Restoretest.
- Änderungen an `.env.production`, Docker-Secrets, Caddy/Nginx, systemd,
  Firewall, Datenbank, Volumes oder Publishing-Gates gelten als eigenständige
  Produktionsänderungen und benötigen ausdrückliche Freigabe.
- Produktive ungetrackte Dateien nicht löschen, verschieben, umbenennen,
  überschreiben oder versehentlich stagen. Erst Zweck, Eigentümer, Rechte und
  Wiederherstellbarkeit klären.
- Bei einem fehlgeschlagenen Check sicher anhalten. Keine Schutzprüfung durch
  manuelle Datenbankänderungen, erweiterte Dateirechte oder temporär gelockerte
  Gates umgehen.
- Not-Aus und pausierte Gates sind sicherheitsrelevanter Zustand. Änderungen
  daran nur als ausdrücklich angekündigten, nachvollziehbaren Schritt ausführen.

## Datenbankmigrationen

- Alembic ist die produktive Schemaquelle. Bestehende, bereits veröffentlichte
  Migrationen nicht nachträglich ändern.
- Neue Schemaänderungen als nächste lineare Revision mit korrektem
  `down_revision` anlegen und Upgrade/Downgrade mindestens auf einer Kopie oder
  Wegwerfdatenbank prüfen.
- Tenant-FKs, tenantbezogene Unique Constraints, Idempotenzschlüssel und
  Status-Check-Constraints ausdrücklich testen.
- Produktive Downgrades nie als improvisierten Rollback verwenden; Restore aus
  einem geprüften Backup ist der sichere Rückweg.
- Stand 1. September 2026 ist die Dateikette linear bis Revision `0034`
  (`fupa_browser_sessions`). Vor jeder Aussage zum aktuellen Head erneut
  `alembic heads` und auf dem Zielsystem `alembic current` prüfen.

## Umgebungsdateien und Secrets

Nur die getrackten `*.example`-Dateien dokumentieren. Reale Werte gehören in
lokale/VPS-Dateien und Docker-Secrets. Besonders beachten: `.gitignore`
ignoriert aktuell `.env` und `.env.staging`, aber nicht pauschal
`.env.production`, `.env.meta-test` oder deren Sicherungskopien. Solche Dateien
deshalb nie versehentlich stagen.

Bei Bestandsaufnahmen nur Variablenschlüssel oder bewusst ausgewählte,
nicht-sensitive Feature-Gates ausgeben. Secret-Dateien höchstens über Namen,
Rechte und Vorhandensein prüfen. Wichtige Secret-Gruppen sind Datenbank,
Session, OpenAI, Meta-App, Tokenverschlüsselung, Webhook, SMTP und gegebenenfalls
Telegram/FuPa-Sitzungsdaten.

## Deployment auf den produktiven VPS

Produktionsziel: `andi@31.70.111.111:/opt/socialmediaagent`.
Die produktive Compose-Kombination ist:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.production.yml COMMAND
```

Bekannter Aufbau: Caddy terminiert öffentlich 80/443; Dashboard-Proxy 8083 und
öffentlicher Callback-/Webhook-Proxy 8084 lauschen nur auf Loopback. Persistente
Daten liegen unter `/srv/social-media-agent-production`, Backups unter
`/srv/social-media-agent-production-backups`, Secrets unter
`/etc/social-media-agent/production-secrets`.

Ein Deployment ist immer ein eigener, ausdrücklich beauftragter Vorgang:

1. lokalen/GitHub-`main`-Commit und Zielcommit notieren;
2. VPS-Status, aktuellen Commit und ungetrackte Dateien lesend prüfen;
3. aktuelles, wiederherstellbares Backup und Restorefähigkeit bestätigen;
4. auf dem VPS `main` ausschließlich per Fast-Forward von dessen GitHub-Remote
   aktualisieren;
5. `config -q`, Build/Start der Production-Compose-Kombination und erfolgreichen
   `migrate`-Dienst prüfen;
6. `alembic current` gegen `alembic heads` vergleichen;
7. `scripts/production-check.sh`, Containerstatus, Worker-Heartbeat und
   öffentliche Proxygrenzen prüfen;
8. `/health` inhaltlich auf `"status":"ok"` prüfen. HTTP 200 allein genügt
   nicht, weil der Endpunkt auch bei `degraded` mit 200 antwortet;
9. bei Publishing-/Provideränderungen zunächst pausierte Gates bzw. Dry-Run
   verwenden und erst nach fachlicher Abnahme einzeln aktivieren.

`scripts/backup.sh` verwendet die Basis-Compose-Konfiguration und ist nicht
automatisch ein vollständiger Produktionsbackup-Workflow. Vor seinem Einsatz
gegen Produktion Compose-Dateien, Zielpfad, enthaltene Daten, Secret-Ausschluss
und dokumentierten Restoreweg prüfen; keinen ungeprüften Backupbefehl als
Sicherheitsnachweis behandeln.

Der Benutzer `andi` kann Docker derzeit weder direkt noch per `sudo -n` lesen;
interaktive sudo-Unterstützung kann erforderlich sein. Keine Umgehung über
Dateirechte, `/proc` oder Secret-Zugriffe versuchen.

## Datiertes Wiederaufnahmeprotokoll (neu prüfen, nicht als Dauerzustand lesen)

Bestandsaufnahme vom 1. September 2026:

- lokaler `main`, GitHub-`main` und VPS-`main` standen auf `ae5d536` (PR #122);
- lokaler Worktree war sauber; auf dem VPS lagen nur ungetrackte reale
  `.env`-/Backup-Dateien und eine leere Datei `grep`, aber keine getrackten
  Änderungen;
- alle Production-Compose-Dienste liefen, ihre Container-Healthchecks waren
  grün und der Migrationscontainer war beim letzten Deployment mit Exit 0
  beendet; `/health` meldete dennoch `degraded` bei Web, Datenbank und Worker
  `ok`;
- der lesende Produktionscheck bestätigte Alembic-Head `0034`, Worker-/
  Scheduler-Gates, FUSSBALL.DE-Worker-Gates, Publishing-Gates und Proxygrenzen;
- die Codeprüfung zeigte einen wahrscheinlichen systematischen Fehler im
  FUSSBALL.DE-Stalenesscheck: normale Mannschaftssynchronisierung kann alle 24
  Stunden geplant sein, der Systemstatus wertet bei globalen 1.800 Sekunden
  aber bereits mehr als 90 Minuten ohne Erfolg als kritisch. Ein Zeitstempel
  des Snapshot-Wurzelverzeichnisses beweist den letzten erfolgreichen Sync
  nicht, weil automatische Snapshots in Mannschafts-Unterordnern liegen. Die
  exakte Critical-Liste und eine möglicherweise zusätzlich ungesunde Social-
  Media-Verbindung blieben ohne geschützte Systemstatus-/DB-Leseabfrage
  unbestätigt;
- Produktion hatte OpenAI-Text/Bild, FUSSBALL.DE-Sync, automatische
  Beitragserzeugung und Meta-Scheduler aktiviert; Multi-Tenancy, Billing und
  Selbstregistrierung waren deaktiviert;
- `FUPA_BROWSER_PUBLISH_ENABLED=true` erlaubte die manuell bestätigte
  Browserübergabe; automatische FuPa-Bericht-Gates waren in der produktiven
  Umgebungsdatei nicht gesetzt und fielen damit auf deaktivierte Defaults;
- GitHub hatte keine Check-Runs für den aktuellen Commit, keine Branch
  Protection, keine offenen Issues und nur den veralteten offenen PR #1;
- lokal war keine `.venv` vorhanden; System-Python 3.14 hatte weder Pytest noch
  Ruff, daher wurde die Suite bei dieser Bestandsaufnahme nicht ausgeführt;
- `andi` hatte keinen direkten Docker-/Dateizugriff, aber eng begrenzte
  passwortlose Wrapper für `socialmedia-admin status`, `check` und dienstbezogene
  Logs. Bei der Bestandsaufnahme wurde nur der nachweislich lesende
  `status`-Unterbefehl verwendet.

Diese Punkte vor Folgemaßnahmen verifizieren. Besonders offen sind die exakte
Critical-Liste des degradierten Healthchecks, die Abstimmung des Health-
Stalenessfensters auf das mannschaftsbezogene Syncintervall, der nachweisbare
aktuelle Alembic-Stand auf dem VPS, Backupfrische/Restoretest, VPS-Dateihygiene,
CI/Branch-Protection sowie die in den Fachdokumenten beschriebenen Grenzen
(inoffizielle FuPa-Browserstrecke, Prompt-Retention, historische
Medienmigration, fehlendes Spielermodell und begrenzte WhatsApp-/Live-Provider-
Funktionen).
