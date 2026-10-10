# B multipart upload verification — 8 October 2026

Branch: `b/sprint4-multipart-upload`, based on `d964075`.

## Executed

Environment: WSL Ubuntu, Python 3.12.3, project-local `.venv`. Dependencies
installed from `pyproject.toml`, including boto3 1.43.109 and Ruff 0.16.10.

- `python -m pytest -q --cov=app`: **467 passed, 65 skipped**, coverage **88.71%**
  (required: 80%). Skipped tests require live services. One existing JWT test
  warns about its intentionally short test key.
- `python -m ruff check .`: passed.
- `python -m ruff format --check` over all **18 changed/new Python files**: passed.
- `python -m alembic upgrade head --sql`: passed; the new migration follows
  `20261004_04`. This generates SQL offline and does not modify a database.
- `git diff --check`: passed before committing.

Full-repository `ruff format --check .` reports **24 existing files** needing
formatting with this installed Ruff version. Those files are outside this
change; the branch does not claim that repository-wide format gate is green.

## Added coverage

Unit tests cover strict schemas, a 1 GiB *declared size* (metadata only),
part signing and pagination, bounded range reads, MIME metadata safety,
owner isolation, anonymous access, invalid ETags/sizes, idempotent completion,
lost storage responses, Redis failures, resume, cancellation and expired-session
cleanup. The 1 GiB metadata case does **not** transfer a 1 GiB video.

Live tests are provided in `tests/integration/test_multipart_postgres.py` and
`tests/integration/test_multipart_storage_live.py`, with the project's required
`RUN_POSTGRES_TESTS` guard. They cover SQL locking/rollback, orphan protection,
real presigned PUTs, list-parts, completion, range reads and abort.

## Not executed / deployment requirements

Docker CLI is installed, but its `desktop-linux` engine is unavailable
(`dockerDesktopLinuxEngine` named pipe missing). Consequently no isolated
PostgreSQL/MinIO stack was available; the live tests, local MinIO configuration
script and browser acceptance have **not** been executed here.

Before merging/deploying:

1. Run the new migration against an isolated test PostgreSQL instance.
2. Run the two new integration modules with `RUN_POSTGRES_TESTS=1`, pointing
   `POSTGRES_DSN` and storage settings at test services.
3. Verify browser PUT CORS and readable ETag with local MinIO, then repeat
   against the configured S3 staging bucket.
4. With E's UI, upload a real 1 GiB video, interrupt/resume it and play its
   processed result. Confirm cancellation and the old `/upload` still work.

No production settings, remote branches, PRs or deployments were changed.
