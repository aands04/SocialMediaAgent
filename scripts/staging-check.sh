#!/bin/sh
set -u
COMPOSE="docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml"
failures=0
check(){ name="$1"; shift; if "$@" >/tmp/staging-check.out 2>&1; then echo "[OK] $name"; else echo "[FEHLER] $name: $(cat /tmp/staging-check.out)"; failures=$((failures+1)); fi; }
check "Compose-Konfiguration" sh -c "$COMPOSE config -q"
check "Webanwendung erreichbar" sh -c "curl -fsS --max-time 10 http://127.0.0.1:${HTTP_PORT:-8080}/health"
check "Anmeldeseite und Session erreichbar" sh -c "curl -fsS --max-time 10 http://127.0.0.1:${HTTP_PORT:-8080}/login | grep -q csrf_token"
check "PostgreSQL erreichbar" sh -c "$COMPOSE exec -T db pg_isready -U socialmedia -d socialmedia"
check "Aktuelle Alembic-Migration installiert" sh -c "test \"\$($COMPOSE exec -T web alembic current | tail -1 | awk '{print \$1}')\" = \"0002\""
check "Worker aktiv und Heartbeat frisch" sh -c "$COMPOSE exec -T worker python -c 'import json; from datetime import datetime,timezone; d=json.load(open(\"/app/data/logs/worker-heartbeat.json\")); assert (datetime.now(timezone.utc)-datetime.fromisoformat(d[\"at\"])).total_seconds()<90'"
check "Scheduler aktiv" sh -c "$COMPOSE exec -T worker python -c 'import json; assert json.load(open(\"/app/data/logs/worker-heartbeat.json\"))[\"scheduler\"] is True'"
check "SMB-Mount vorhanden und lesbar" sh -c "$COMPOSE exec -T web test -r /app/external-media/staging_smoke/spieler/smoke-player.png"
check "SMB-Mount im Container read-only" sh -c "! $COMPOSE exec -T web sh -c 'touch /app/external-media/.write-test'"
check "Testbild technisch lesbar" sh -c "$COMPOSE exec -T web python -c 'from PIL import Image; Image.open(\"/app/external-media/staging_smoke/spieler/smoke-player.png\").verify()'"
check "Path-Traversal blockiert" sh -c "$COMPOSE exec -T web python scripts/check_path_traversal.py"
check "Feed-/Story-Verzeichnis beschreibbar" sh -c "$COMPOSE exec -T web sh -c 'mkdir -p /app/data/generated/feed /app/data/generated/story && touch /app/data/generated/feed/.check /app/data/generated/story/.check && rm /app/data/generated/feed/.check /app/data/generated/story/.check'"
check "Provider-Snapshot-Verzeichnis beschreibbar" sh -c "$COMPOSE exec -T web sh -c 'touch /app/data/provider-snapshots/.check && rm /app/data/provider-snapshots/.check'"
check "DryRun aktiv, Live-Publishing technisch deaktiviert" sh -c "$COMPOSE exec -T web python -c 'from app.config import get_settings; s=get_settings(); assert s.environment==\"staging\" and s.publisher_mode==\"dry-run\" and not s.global_publish_enabled and not s.meta_access_token'"
check "Globaler Not-Aus in Datenbank funktionsfähig" sh -c "$COMPOSE exec -T web python -c 'from app.db import SessionLocal; from app.models import SystemSetting; d=SessionLocal(); x=d.get(SystemSetting,\"emergency_stop\") or SystemSetting(key=\"emergency_stop\",value={\"enabled\":False}); d.add(x); x.value={\"enabled\":True}; d.commit(); assert d.get(SystemSetting,\"emergency_stop\").value[\"enabled\"]; x.value={\"enabled\":False}; d.commit(); d.close()'"
check "FUSSBALL.DE-Live-Modus standardmäßig deaktiviert" sh -c "$COMPOSE exec -T web python -c 'from app.config import get_settings; assert not get_settings().fussball_live_test_enabled'"
check "Backupziel beschreibbar" sh -c "test -d \"${STAGING_BACKUP_ROOT}\" && touch \"${STAGING_BACKUP_ROOT}/.check\" && rm \"${STAGING_BACKUP_ROOT}/.check\""
rm -f /tmp/staging-check.out
if [ "$failures" -gt 0 ]; then echo "[KRITISCH] $failures Staging-Prüfungen fehlgeschlagen."; exit 1; fi
echo "[OK] Alle kritischen Staging-Prüfungen bestanden."
