#!/bin/sh
set -eu

# Make sure the database directory exists before gunicorn starts.  When the
# named volume is empty it inherits the image's /app/data ownership (the
# unprivileged "archive" user), so no root-time chown is needed at runtime.
DB_PATH="${ARCHIVE_DB:-/app/data/archive.sqlite3}"
mkdir -p "$(dirname "$DB_PATH")"

if [ ! -w "$(dirname "$DB_PATH")" ]; then
    echo "archive: database directory $(dirname "$DB_PATH") is not writable" >&2
    exit 1
fi

exec "$@"
