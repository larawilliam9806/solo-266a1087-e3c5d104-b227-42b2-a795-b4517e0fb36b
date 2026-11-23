#!/bin/sh
set -eu

# Make sure the database directory exists and is owned by the runtime user.
DB_PATH="${ARCHIVE_DB:-/app/data/archive.sqlite3}"
mkdir -p "$(dirname "$DB_PATH")"

exec "$@"
