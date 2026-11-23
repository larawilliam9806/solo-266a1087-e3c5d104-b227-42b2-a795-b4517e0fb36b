"""WSGI entry point for `gunicorn archive.wsgi:app`."""

import os

from .app import create_app

app = create_app(
    {
        "DATABASE": os.environ.get("ARCHIVE_DB", "data/archive.sqlite3"),
    }
)
