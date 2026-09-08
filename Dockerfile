FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations

RUN python -m pip install --no-cache-dir .

# Drop root for the runtime. The install above needs it to write into the
# system site-packages; nothing after it does. /app deliberately stays owned by
# root and read-only to this user -- the process reads its own code and never
# writes it, and PYTHONDONTWRITEBYTECODE stops Python trying to drop .pyc files
# beside it. Uploads spool to /tmp, which stays writable, and binding port 8000
# needs no privilege. Both the api and worker services run from this image, so
# both drop root together.
RUN useradd --system --create-home --uid 10001 flickpond
USER flickpond

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]

