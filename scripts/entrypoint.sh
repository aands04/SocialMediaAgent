#!/bin/sh
set -eu
if [ -r /run/secrets/session_secret ]; then export SESSION_SECRET="$(cat /run/secrets/session_secret)"; fi
if [ -r /run/secrets/openai_api_key ]; then export OPENAI_API_KEY="$(cat /run/secrets/openai_api_key)"; fi
if [ -r /run/secrets/meta_app_id ]; then export META_APP_ID="$(cat /run/secrets/meta_app_id)"; fi
if [ -r /run/secrets/meta_app_secret ]; then export META_APP_SECRET="$(cat /run/secrets/meta_app_secret)"; fi
case "${ENVIRONMENT:-development}" in production|meta-test) unset META_FACEBOOK_APP_SECRET ;; esac
if [ -s /run/secrets/meta_facebook_app_secret ]; then export META_FACEBOOK_APP_SECRET="$(cat /run/secrets/meta_facebook_app_secret)"; fi
if [ -r /run/secrets/meta_token_encryption_key ]; then export META_TOKEN_ENCRYPTION_KEY="$(cat /run/secrets/meta_token_encryption_key)"; fi
if [ -r /run/secrets/meta_webhook_verify_token ]; then export META_WEBHOOK_VERIFY_TOKEN="$(cat /run/secrets/meta_webhook_verify_token)"; fi
if [ -s /run/secrets/smtp_password ]; then export SMTP_PASSWORD="$(cat /run/secrets/smtp_password)"; fi
if [ "${DATABASE_URL:-auto}" = "auto" ]; then
  test -r /run/secrets/db_password || { echo "KRITISCH: DB-Secret fehlt" >&2; exit 1; }
  encoded="$(python -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip(), safe=""))' < /run/secrets/db_password)"
  export DATABASE_URL="postgresql+psycopg://socialmedia:${encoded}@db:5432/socialmedia"
fi
for directory in "${GENERATED_ROOT:-/app/data/generated}" "${PROVIDER_SNAPSHOT_ROOT:-/app/data/provider-snapshots}" "${LOG_ROOT:-/app/data/logs}"; do
  mkdir -p "$directory"
  test -w "$directory" || { echo "KRITISCH: $directory ist nicht beschreibbar" >&2; exit 1; }
done
if [ "${REQUIRE_MEDIA_MOUNT:-false}" = "true" ]; then
  test -d "${MEDIA_ROOT:-/app/external-media}" || { echo "KRITISCH: SMB-Mount fehlt" >&2; exit 1; }
  test -r "${MEDIA_ROOT:-/app/external-media}" || { echo "KRITISCH: SMB-Mount nicht lesbar" >&2; exit 1; }
fi
exec "$@"
