#!/bin/sh
set -u

failures=0

ok() { printf '[OK] %s\n' "$1"; }
fail() { printf '[FEHLER] %s\n' "$1" >&2; failures=$((failures + 1)); }

check() {
  label="$1"
  shift
  if "$@" >/tmp/meta-test-check.out 2>&1; then
    ok "$label"
  else
    fail "$label: $(tail -n 4 /tmp/meta-test-check.out | tr '\n' ' ')"
  fi
}

test -f .env.meta-test || {
  echo "[KRITISCH] .env.meta-test fehlt" >&2
  exit 1
}

set -a
. ./.env.meta-test
set +a

COMPOSE="docker compose --env-file .env.meta-test -f docker-compose.yml -f docker-compose.meta-test.yml"

check "Compose-Konfiguration" sh -c "$COMPOSE config --quiet"

[ "${ENVIRONMENT:-}" = "meta-test" ] &&
  [ "${PUBLISHER_MODE:-}" = "instagram" ] &&
  [ "${META_TEST_ENABLED:-}" = "true" ] &&
  [ "${META_SCHEDULER_ENABLED:-}" = "false" ] &&
  [ "${GLOBAL_PUBLISH_ENABLED:-}" = "false" ] &&
  ok "Harte Meta-Test-Umgebungsgates" ||
  fail "Meta-Test-Umgebungsgates sind nicht sicher gesetzt"
case "${META_TEST_PUBLISH_ENABLED:-}" in
  true|false) ok "Explizites externes Meta-Gate ist gesetzt (${META_TEST_PUBLISH_ENABLED})" ;;
  *) fail "META_TEST_PUBLISH_ENABLED muss ausdrücklich true oder false sein" ;;
esac

case "${META_OAUTH_REDIRECT_URI:-}" in
  https://*/public/instagram/oauth/callback) ok "OAuth-Redirect verwendet HTTPS und exakten Callback-Pfad" ;;
  *) fail "META_OAUTH_REDIRECT_URI muss eine öffentliche HTTPS-Callback-URL sein" ;;
esac
case "${META_PUBLIC_BASE_URL:-}" in
  https://*) ok "Öffentliche Medienbasis verwendet HTTPS" ;;
  *) fail "META_PUBLIC_BASE_URL muss mit https:// beginnen" ;;
esac

for name in db_password session_secret meta_app_id meta_app_secret meta_token_encryption_key; do
  path="${META_TEST_SECRETS_ROOT:-/nonexistent}/$name"
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

check "Webanwendung erreichbar" curl -fsS "http://127.0.0.1:${HTTP_PORT:-8080}/health"
check "Instagram-Verwaltung erreichbar" curl -fsS "http://127.0.0.1:${HTTP_PORT:-8080}/login"
check "Aktuelle Alembic-Migration installiert" sh -c \
  "$COMPOSE exec -T web /app/scripts/entrypoint.sh alembic current | grep -q '0006 (head)'"
check "Worker läuft ohne automatischen Meta-Scheduler" sh -c \
  "$COMPOSE exec -T worker sh -c 'test \"\$META_SCHEDULER_ENABLED\" = false'"
check "Web sieht Meta-Secrets, ohne sie auszugeben" sh -c \
  "$COMPOSE exec -T web sh -c 'test -n \"\$META_APP_ID\" && test -n \"\$META_APP_SECRET\" && test -n \"\$META_TOKEN_ENCRYPTION_KEY\"'"
check "Tokenverschlüsselungsschlüssel ist gültig" sh -c \
  "$COMPOSE exec -T web python -c 'from app.meta.security import TokenCipher; from app.config import get_settings; TokenCipher(get_settings().meta_token_encryption_key)'"

public_status="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${META_PUBLIC_PORT:-8081}/" 2>/dev/null || true)"
[ "$public_status" = "404" ] &&
  ok "Öffentlicher Proxy blockiert das Dashboard" ||
  fail "Öffentlicher Proxy muss / mit 404 blockieren (erhalten: $public_status)"
callback_status="$(curl -sS -o /dev/null -w '%{http_code}' \
  "http://127.0.0.1:${META_PUBLIC_PORT:-8081}/public/instagram/oauth/callback" \
  2>/dev/null || true)"
[ "$callback_status" = "400" ] &&
  ok "Öffentlicher Proxy reicht ausschließlich den OAuth-Callback durch" ||
  fail "OAuth-Callback muss ohne Parameter kontrolliert mit 400 antworten (erhalten: $callback_status)"

if [ "$failures" -gt 0 ]; then
  printf '[KRITISCH] %s Meta-Test-Prüfungen fehlgeschlagen.\n' "$failures" >&2
  exit 1
fi
ok "Alle kritischen Meta-Test-Prüfungen bestanden"
