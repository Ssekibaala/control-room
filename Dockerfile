FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Northflank (like Render) injects PORT at runtime and expects the
# service to bind to it - $PORT is not known at build time, so it has
# to be read in the shell form of CMD, not the exec-form array (which
# would pass the literal string "$PORT" to gunicorn instead of
# expanding it).
CMD gunicorn app:app --bind 0.0.0.0:${PORT:-8080}
