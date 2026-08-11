#!/bin/sh
set -u

failures=0
ok() { printf '[OK] %s\n' "$1"; }
fail() { printf '[FEHLER] %s\n' "$1" >&2; failures=$((failures + 1)); }
check() {
  label="$1"
  shift
  if "$@" >/tmp/production-check.out 2>&1; then
    ok "$label"
  else
    fail "$label: $(tail -n 4 /tmp/production-check.out | tr '\n' ' ')"
  fi
}

test -f .env.production || {
  echo "[KRITISCH] .env.production fehlt" >&2
  exit 1
}

set -a
. ./.env.production
set +a
COMPOSE="docker compose --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml"

check_alembic_head() {
  installed="$($COMPOSE exec -T web /app/scripts/entrypoint.sh alembic current 2>/dev/null | awk '/\(head\)/ { print $1 }' | sort)"
  available="$($COMPOSE exec -T web /app/scripts/entrypoint.sh alembic heads 2>/dev/null | awk '/\(head\)/ { print $1 }' | sort)"
  test -n "$installed" && test "$installed" = "$available"
}

check "Compose-Konfiguration" sh -c "$COMPOSE config --quiet"

[ "${ENVIRONMENT:-}" = "production" ] &&
  [ "${PUBLISHER_MODE:-}" = "instagram" ] &&
  [ "${META_PRODUCTION_ENABLED:-}" = "true" ] &&
  [ "${META_TEST_ENABLED:-}" = "false" ] &&
  [ "${META_TEST_PUBLISH_ENABLED:-}" = "false" ] &&
  [ -z "${META_ACCESS_TOKEN:-}" ] &&
  [ -z "${META_FACEBOOK_APP_SECRET:-}" ] &&
  ok "Harte Produktions-Umgebungsgates" ||
  fail "Produktions-Umgebungsgates sind nicht korrekt"

flags="${GLOBAL_PUBLISH_ENABLED:-false}:${META_SCHEDULER_ENABLED:-false}:${META_AUTOMATIC_PUBLISH_ENABLED:-false}"
case "$flags" in
  false:false:false) ok "Automatik vollständig pausiert" ;;
  true:true:true) ok "Automatik mit allen drei Gates aktiviert" ;;
  *) fail "Automatik-Gates sind nur gemeinsam true oder gemeinsam false zulässig ($flags)" ;;
esac

connection_check_interval="${META_CONNECTION_CHECK_INTERVAL_SECONDS:-43200}"
connection_max_age="${META_CONNECTION_MAX_AGE_SECONDS:-86400}"
if [ "$connection_check_interval" -gt 0 ] 2>/dev/null &&
  [ "$connection_check_interval" -le 43200 ] 2>/dev/null &&
  [ "$connection_check_interval" -lt "$connection_max_age" ] 2>/dev/null; then
  ok "Automatische Instagram-Verbindungsprüfung mindestens zweimal täglich"
else
  fail "Instagram-Verbindungsprüfung muss positiv, höchstens 43200 Sekunden und jünger als META_CONNECTION_MAX_AGE_SECONDS sein"
fi

fussball_flags="${FUSSBALL_AUTOMATIC_SYNC_ENABLED:-false}:${AUTOMATIC_POST_GENERATION_ENABLED:-false}"
case "$fussball_flags" in
  false:false) ok "FUSSBALL.DE-Automatik vollständig pausiert" ;;
  true:false) ok "FUSSBALL.DE-Sync aktiv; automatische Entwürfe pausiert" ;;
  true:true) ok "FUSSBALL.DE-Sync und automatische Entwürfe aktiviert" ;;
  *) fail "Automatische Entwürfe benötigen den FUSSBALL.DE-Sync ($fussball_flags)" ;;
esac

case "${META_OAUTH_REDIRECT_URI:-}" in
  https://*/public/instagram/oauth/callback) ok "OAuth-Redirect verwendet HTTPS" ;;
  *) fail "META_OAUTH_REDIRECT_URI ist ungültig" ;;
esac
case "${META_PUBLIC_BASE_URL:-}" in
  https://*) ok "Öffentliche Medienbasis verwendet HTTPS" ;;
  *) fail "META_PUBLIC_BASE_URL muss mit https:// beginnen" ;;
esac

if [ "${PASSWORD_RESET_ENABLED:-false}" = "true" ]; then
  case "${APP_PUBLIC_BASE_URL:-}" in
    https://*) ok "Passwort-Reset verwendet eine öffentliche HTTPS-Basis" ;;
    *) fail "APP_PUBLIC_BASE_URL muss für Passwort-Reset mit https:// beginnen" ;;
  esac
  [ -n "${SMTP_HOST:-}" ] && [ -n "${SMTP_FROM_EMAIL:-}" ] &&
    ok "SMTP-Konfiguration für Passwort-Reset vorhanden" ||
    fail "SMTP_HOST und SMTP_FROM_EMAIL fehlen"
  smtp_secret="${SMTP_PASSWORD_FILE_HOST:-/dev/null}"
  if [ -n "${SMTP_USERNAME:-}" ] && [ ! -s "$smtp_secret" ]; then
    fail "SMTP-Passwortdatei fehlt oder ist leer"
  else
    ok "SMTP-Passwortdatei ist passend konfiguriert"
  fi
else
  ok "Passwort-Reset bewusst deaktiviert"
fi

for name in db_password session_secret openai_api_key meta_app_id meta_app_secret meta_token_encryption_key; do
  path="${PRODUCTION_SECRETS_ROOT:-/nonexistent}/$name"
  if [ -s "$path" ]; then
    mode="$(stat -c %a "$path" 2>/dev/null || echo 999)"
    case "$mode" in
      400|440|600|640) ok "Secret $name vorhanden und eingeschränkt ($mode)" ;;
      *) fail "Secret $name hat zu offene Rechte ($mode)" ;;
    esac
  else
    fail "Secret $name fehlt oder ist leer"
  fi
done

if [ -n "${META_FACEBOOK_APP_ID:-}" ] || [ -n "${META_WHATSAPP_CONFIGURATION_ID:-}" ]; then
  [ -n "${META_FACEBOOK_APP_ID:-}" ] &&
    ok "Separate Meta-App-ID für Facebook und WhatsApp vorhanden" ||
    fail "META_FACEBOOK_APP_ID fehlt für Facebook/WhatsApp"
  facebook_secret="${META_FACEBOOK_APP_SECRET_FILE_HOST:-/dev/null}"
  if [ -s "$facebook_secret" ]; then
    mode="$(stat -c %a "$facebook_secret" 2>/dev/null || echo 999)"
    case "$mode" in
      400|440|600|640) ok "Separater Meta-App-Geheimcode vorhanden und eingeschränkt ($mode)" ;;
      *) fail "Separater Meta-App-Geheimcode hat zu offene Rechte ($mode)" ;;
    esac
  else
    fail "META_FACEBOOK_APP_SECRET_FILE_HOST fehlt oder ist leer"
  fi
else
  ok "Facebook-/WhatsApp-App-Zugang bewusst noch nicht eingerichtet"
fi

check "Webanwendung erreichbar" curl -fsS "http://127.0.0.1:${HTTP_PORT:-8083}/health"
check "Anmeldeseite erreichbar" sh -c "curl -fsS http://127.0.0.1:${HTTP_PORT:-8083}/login | grep -q csrf_token"
check "Datenschutzseite öffentlich erreichbar" sh -c "curl -fsS http://127.0.0.1:${HTTP_PORT:-8083}/datenschutz | grep -q Datenschutzerklärung"
check "Datenlöschungsseite öffentlich erreichbar" sh -c "curl -fsS http://127.0.0.1:${HTTP_PORT:-8083}/datenloeschung | grep -q Datenlöschung"
check "Aktuelle Alembic-Migration installiert" check_alembic_head
check "Worker-Modus stimmt mit den Gates überein" sh -c \
  "$COMPOSE exec -T worker /app/scripts/entrypoint.sh python -c 'import json; from app.config import get_settings; s=get_settings(); d=json.load(open(s.log_root / \"worker-heartbeat.json\")); expected=s.global_publish_enabled and s.meta_scheduler_enabled and s.meta_automatic_publish_enabled; assert d[\"automatic_scheduler\"] is expected'"
check "FUSSBALL.DE-Worker-Gates stimmen überein" sh -c \
  "$COMPOSE exec -T worker /app/scripts/entrypoint.sh python -c 'import json; from app.config import get_settings; s=get_settings(); d=json.load(open(s.log_root / \"worker-heartbeat.json\")); assert d[\"automatic_fussball_sync\"] is s.fussball_automatic_sync_enabled; assert d[\"automatic_post_generation\"] is (s.fussball_automatic_sync_enabled and s.automatic_post_generation_enabled)'"
check "Tokenverschlüsselungsschlüssel ist gültig" sh -c \
  "$COMPOSE exec -T web /app/scripts/entrypoint.sh python -c 'from app.meta.security import TokenCipher; from app.config import get_settings; TokenCipher(get_settings().meta_token_encryption_key)'"

public_status="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${META_PUBLIC_PORT:-8084}/" 2>/dev/null || true)"
[ "$public_status" = "404" ] &&
  ok "Öffentlicher Proxy blockiert das Dashboard" ||
  fail "Öffentlicher Proxy muss / mit 404 blockieren (erhalten: $public_status)"
callback_status="$(curl -sS -o /dev/null -w '%{http_code}' \
  "http://127.0.0.1:${META_PUBLIC_PORT:-8084}/public/instagram/oauth/callback" 2>/dev/null || true)"
[ "$callback_status" = "400" ] &&
  ok "OAuth-Callback ist ausschließlich am öffentlichen Proxy erreichbar" ||
  fail "OAuth-Callback muss ohne Parameter kontrolliert 400 liefern (erhalten: $callback_status)"

rm -f /tmp/production-check.out
if [ "$failures" -gt 0 ]; then
  printf '[KRITISCH] %s Produktionsprüfungen fehlgeschlagen.\n' "$failures" >&2
  exit 1
fi
ok "Alle kritischen Produktionsprüfungen bestanden"
