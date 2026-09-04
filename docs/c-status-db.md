# C Track: Status Endpoints and Database

## Docker-first setup in WSL

From `/mnt/e/workspace/Media_player`:

```bash
cp .env.example .env
docker compose up -d postgres minio redis
docker compose run --rm --service-ports \
  -v "$PWD:/workspace" -w /workspace api \
  sh -c "python -m pip install -e . && alembic upgrade head && \
         uvicorn app.main:app --host 0.0.0.0 --port 8000"
```

The API is available at `http://127.0.0.1:8000`, and its OpenAPI UI is at `http://127.0.0.1:8000/docs`.

This route uses the project's Python 3.12 container and does not require Python packages to be installed globally in WSL.

`MINIO_ENDPOINT` is the service-to-service address, normally `minio:9000` in Compose. `MINIO_PUBLIC_ENDPOINT` is embedded in signed playback URLs and must be reachable by the browser, normally `127.0.0.1:9000` for local development. `MINIO_REGION` must match the object store and defaults to `us-east-1` locally.

## Migration checks

Apply the migration from an empty database:

```bash
docker compose run --rm -v "$PWD:/workspace" -w /workspace api \
  sh -c "python -m pip install -e . && alembic upgrade head && alembic current"
```

To verify that the migration is reversible in a disposable local database:

```bash
docker compose run --rm -v "$PWD:/workspace" -w /workspace api \
  sh -c "python -m pip install -e . && alembic downgrade base && alembic upgrade head"
```

## Tests

```bash
docker compose run --rm -v "$PWD:/workspace" -w /workspace api \
  sh -c "python -m pip install -e '.[dev]' && \
         RUN_POSTGRES_TESTS=1 pytest --cov=app"
```

API tests replace the database and MinIO dependencies with fakes. Repository integration is checked separately against the PostgreSQL service during Sprint integration.
