#!/bin/sh
# Run from the project root. Requires Docker Compose and a configured .env.
set -eu
umask 077
backup_dir="backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
docker compose --env-file .env -f deploy/compose.yaml stop api worker
trap 'docker compose --env-file .env -f deploy/compose.yaml start api worker' EXIT
docker compose --env-file .env -f deploy/compose.yaml exec -T db pg_dump -U hypit -d hypit -Fc > "$backup_dir/database.dump"
docker compose --env-file .env -f deploy/compose.yaml run --rm --no-deps -T api tar -C /data -czf - . > "$backup_dir/media.tar.gz"
test -s "$backup_dir/database.dump"
test -s "$backup_dir/media.tar.gz"
echo "Backup saved in $backup_dir (contains private task data and signing key)."
