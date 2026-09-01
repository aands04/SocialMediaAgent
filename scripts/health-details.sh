#!/bin/sh
set -eu

if [ "${DATABASE_URL:-auto}" = "auto" ]; then
  test -r /run/secrets/db_password || {
    echo "Sanitizierte Health-Diagnose ist nicht verfügbar." >&2
    exit 1
  }
  encoded="$(
    python -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip(), safe=""))' \
      < /run/secrets/db_password
  )"
  export DATABASE_URL="postgresql+psycopg://socialmedia:${encoded}@db:5432/socialmedia"
fi

exec python -m app.monitoring.health_details
