"""Gunicorn configuration for multi-worker SQLite use.

The application plans submissions against a read snapshot and only holds the
SQLite write lock for a short commit, so several workers and threads can
serve hand-ins concurrently.  Keeping enough threads per worker lets a large
CSV be planned while other requests (including small orders on the same reel)
are still served.
"""

import multiprocessing
import os

bind = os.environ.get("BIND", "0.0.0.0:8000")
workers = int(os.environ.get("WEB_WORKERS", multiprocessing.cpu_count() * 2 + 1))
threads = int(os.environ.get("WEB_THREADS", 4))
worker_class = "gthread"
# A submission that takes long to compute only holds its own worker thread.
timeout = int(os.environ.get("WEB_TIMEOUT", 300))
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
capture_output = True
