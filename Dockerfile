FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARCHIVE_DB=/app/data/archive.sqlite3

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY archive ./archive
COPY gunicorn.conf.py ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/data \
    && useradd --create-home --uid 10001 archive \
    && chown -R archive:archive /app

USER archive

EXPOSE 8000

# Submissions are planned against a lock-free read snapshot and committed in
# a short BEGIN IMMEDIATE transaction, so multiple workers and threads can
# serve hand-ins concurrently, including appends to the same archive reel.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["gunicorn", "--config", "gunicorn.conf.py", "archive.wsgi:app"]
