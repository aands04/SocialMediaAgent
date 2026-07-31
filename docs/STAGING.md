# Staging auf einer Proxmox-VM

Staging ist eine abgeschottete Generalprobe. `PUBLISHER_MODE=dry-run`, `GLOBAL_PUBLISH_ENABLED=false` und ein leerer `META_ACCESS_TOKEN` werden im Compose-Override erzwungen. Der Smoke-Test instanziiert ausschließlich `DryRunPublisher`. Eine echte Instagram-Veröffentlichung ist in diesem Aufbau technisch nicht konfiguriert.

## 1. Host vorbereiten

Empfohlen: aktuelle Debian-/Ubuntu-VM, 4 vCPU, 8 GiB RAM, 40 GiB Systemdisk plus gesicherte Daten-/Backupziele, feste IP, NTP und Tailscale/VPN. Installieren:

```bash
sudo apt update
sudo apt install -y ca-certificates curl cifs-utils
# Docker Engine und Compose v2 aus dem offiziellen Docker-Repository installieren.
sudo install -d -m 0750 -o 10001 -g 10001 /srv/social-media-agent/{postgres,generated,uploads,provider-snapshots,logs}
sudo install -d -m 0750 /srv/social-media-agent-backups /etc/social-media-agent/secrets
```

Keine mitgelieferten Standardpasswörter verwenden. Secrets einzeln anlegen:

```bash
sudo sh -c 'umask 077; openssl rand -base64 48 > /etc/social-media-agent/secrets/db_password'
sudo sh -c 'umask 077; openssl rand -base64 64 > /etc/social-media-agent/secrets/session_secret'
sudo sh -c 'umask 077; openssl rand -base64 32 > /etc/social-media-agent/secrets/smoke_admin_password'
sudo sh -c 'umask 077; : > /etc/social-media-agent/secrets/openai_api_key' # leer im Mock-Modus
sudo chmod 600 /etc/social-media-agent/secrets/*
```

OpenAI ist standardmäßig vollständig offline: `TEXT_GENERATOR_MODE=mock` und `IMAGE_GENERATOR_MODE=playwright`. Für einen gesonderten, kostenpflichtigen KI-Test `TEXT_GENERATOR_MODE=openai` und/oder `IMAGE_GENERATOR_MODE=openai` setzen und ausschließlich den API-Key in `openai_api_key` hinterlegen. Das Bildmodell wird mit `OPENAI_IMAGE_MODEL=gpt-image-2`, die Qualität mit `OPENAI_IMAGE_QUALITY=medium` festgelegt. Anschließend Web und Worker neu erstellen, die Prompt-Vorschau und genau einen Beitrag testen und alle Schreibweisen, Spieler, Trikots und Logos vor der Freigabe visuell prüfen. Niemals Meta-Tokens anlegen.

## 2. SMB sicher read-only mounten

Credentials liegen nur auf dem Host:

```bash
sudo install -m 600 /dev/null /etc/social-media-agent/smb-credentials
sudoedit /etc/social-media-agent/smb-credentials
# username=...
# password=...
# domain=...
sudo mkdir -p /mnt/social-media-assets
```

`/etc/fstab`:

```text
//SERVER/FREIGABE /mnt/social-media-assets cifs credentials=/etc/social-media-agent/smb-credentials,ro,vers=3.1.1,nosuid,nodev,noexec,serverino,iocharset=utf8,uid=10001,gid=10001,file_mode=0440,dir_mode=0550,_netdev,nofail,x-systemd.automount,x-systemd.device-timeout=15s 0 0
```

Danach `sudo systemctl daemon-reload && sudo mount /mnt/social-media-assets`. Erwartete Struktur:

```text
/mnt/social-media-assets/
├── erste_mannschaft/spieler/
├── zweite_mannschaft/spieler/
├── gemeinsam/{logos,hintergruende,schriftarten}/
└── staging_smoke/spieler/smoke-player.png
```

Mit `findmnt -no OPTIONS /mnt/social-media-assets` muss `ro` sichtbar sein. Docker bind-mountet denselben Pfad nochmals read-only. Fällt SMB aus, darf kein Scan erzwungen werden: Metadaten bleiben bestehen, `reserved_game_id` wird nicht gelöst, Beiträge bleiben unvollständig und der Systemstatus wird kritisch. Nach Wiederkehr `sudo systemctl restart mnt-social\x2dmedia\x2dassets.automount`, Datei lesen, dann im Dashboard neu scannen. Niemals einen leeren lokalen Ersatzordner über den Mountpunkt schreiben lassen.

## 3. Erststart

```bash
cp .env.staging.example .env.staging
chmod 600 .env.staging
# Pfade/IP prüfen
sudo docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml config -q
sudo docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml up -d --build
sudo docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml ps
```

PostgreSQL muss zuerst gesund sein. Danach läuft genau der einmalige `migrate`-Service. Zusätzlich schützt ein PostgreSQL Advisory Lock vor parallelen Migrationen. Erst nach erfolgreicher Migration starten Web und Worker; erst nach gesundem Web startet Nginx. Der Runtime-Entrypoint prüft beschreibbare Volumes und den lesbaren SMB-Mount.

Anschließend:

```bash
. ./.env.staging
sudo -E scripts/staging-check.sh
sudo -E scripts/staging-smoke-test.sh
```

Der Smoke-Test läuft zweimal, verwendet deterministische Schlüssel und erwartet weiterhin genau einen Beitrag, eine Bildreservierung und drei Jobs (Feed plus zwei Storys).

## 4. Kontrollierte FUSSBALL.DE-Diagnose

Standard ist `FUSSBALL_LIVE_TEST_ENABLED=false`. Für ein enges Wartungsfenster auf `true` setzen und nur Web neu erstellen. Ein Administrator öffnet **Provider-Diagnose**, wählt die Mannschaft und bestätigt `NUR LESEN`. HTML, URL, UTC-Zeit, Status und SHA-256 werden gespeichert; Parserdaten und Fixture-Differenz liegen separat in der DB. Die Diagnose liest zusätzlich die verlinkten öffentlichen Spiel-Detailseiten in begrenzter Anzahl, um Platzname, Platzart und Anschrift in die Vorschau aufzunehmen. Es werden keine Spiele übernommen. HTML-Download und Übernahme als neues Fixture sind getrennte Administratoraktionen. Danach Flag wieder deaktivieren. Rate Limits, Nutzungsbedingungen und robots-Vorgaben sind vor dem Abruf zu prüfen.

Vorläufig markierte Spielpläne bleiben standardmäßig für die Automatisierung gesperrt. Soll eine Mannschaft diese Termine regulär für Ankündigungen verwenden, kann ein Administrator unter **Regeln & Storys** die Option **Vorläufige FUSSBALL.DE-Spielpläne für Ankündigungen zulassen** aktivieren und den Snapshot anschließend erneut importieren. Der Providerstatus bleibt sichtbar; abgesagte oder verlegte Spiele werden weiterhin immer blockiert.

## 5. Backup und Restore-Probe

```bash
. ./.env.staging
sudo -E scripts/staging-backup.sh
# Leere, ausschließlich für Restore bestimmte PostgreSQL-Datenbank bereitstellen:
export RESTORE_TEST_DATABASE_URL='postgresql://.../socialmedia_restore_test'
scripts/staging-restore-test.sh /srv/social-media-agent-backups/ZEITSTEMPEL
```

Das Backup enthält `pg_dump`, Uploads, Grafiken, Provider-Snapshots, Logs und nicht geheime Konfigurationsvorlagen. `.env.staging`, SMB-Credentials und Secrets werden nicht archiviert. Restore verweigert eine nicht leere Datenbank und prüft Benutzer, Teams, Medien, Beiträge, Jobs, Audit sowie erzeugte Dateien.

## 6. PostgreSQL-Integrationstests

Eine disposable Datenbank ist zwingend, weil die Tests alle Tabellen löschen:

```bash
export TEST_POSTGRES_URL='postgresql+psycopg://.../socialmedia_test'
pytest -q -m postgresql tests/test_postgresql.py
```

Geprüft werden echte Constraints, `FOR UPDATE SKIP LOCKED`, parallele Bildreservierung, Idempotency Keys und der eindeutige Hauptbeitrag. Niemals gegen Staging-/Produktivdaten ausführen.

## 7. Betrieb und Abbruchkriterien

`/system` zeigt DB, Worker/Scheduler-Heartbeat, SMB, freien Speicher, Provider, OpenAI-Modus, Dry-Run-Gate, Backupalter und Warteschlangen. Kritische Zustände stoppen die Abnahme. Ein HTTP-Healthcheck allein genügt nicht; der Worker-Heartbeat enthält Laufzähler und Schedulerstatus. Bei einem Not-Aus während eines bereits laufenden Requests ist dessen Plattformstatus zu klären; in Staging gibt es ausschließlich Dry-Run.

Vor einem ersten echten Meta-Test fehlen weiterhin: erneute Prüfung der offiziellen Meta-Dokumentation, professionelle Testseite, App Review/Berechtigungen, Token-Lifecycle/Secret-Rotation, öffentlich erreichbare Medien-URLs, Staging-Abnahme der echten Formate und ein separates explizites Live-Deployment. Das Staging-Compose darf dafür nicht wiederverwendet werden.
