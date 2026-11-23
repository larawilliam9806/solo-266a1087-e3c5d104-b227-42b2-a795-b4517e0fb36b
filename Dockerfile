FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARCHIVE_DB=/app/data/archive.sqlite3

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY archive ./archive
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/data

EXPOSE 8000

# Multiple workers share one SQLite file; each submission runs in a single
# BEGIN IMMEDIATE transaction, so concurrent orders serialise safely.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", \
     "--threads", "4", "--timeout", "60", "archive.wsgi:app"]
