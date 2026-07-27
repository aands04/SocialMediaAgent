#!/bin/sh
set -eu
test $# -eq 1 || { echo "Aufruf: $0 BACKUP-ORDNER"; exit 2; }
docker compose exec -T db pg_restore -U socialmedia -d socialmedia --clean --if-exists < "$1/database.dump"
tar -xzf "$1/files.tar.gz"
