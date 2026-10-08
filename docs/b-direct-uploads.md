# B: direct resumable uploads

The browser uploads 16 MiB parts directly to S3-compatible storage. The API
receives metadata only, except a 4096-byte range read after assembly. The old
`POST /upload` still works with its original 100 MiB limit.

## API contract for E

All API requests need the existing login cookie. Through nginx prepend `/api`.
Each upload belongs to its creator, including for operator accounts. Another
owner's ID and an unknown ID both return 404; anonymous requests return 401.

1. `POST /uploads` with `{filename, size, content_type}` returns 201 with
   `{upload_id, part_size, part_count}`. Save the ID alongside the local file
   identity. A session lasts 24 hours; sizes are positive integral bytes with
   at most 10,000 parts (16 MiB each). This is a transport bound, not a duration rule.
2. `POST /uploads/{id}/parts` with `{part_numbers: [1,2,3]}` returns
   `{urls: {"1": "...", "2": "..."}}`. At most 100 distinct numbers per request.
   URLs expire within 15 minutes; request fresh ones as necessary. PUT each raw
   `file.slice((n-1)*part_size, min(n*part_size, file.size))` to its URL.
   Do not send the API cookie to storage. Save the response `ETag` exactly,
   including quotes. Never rewrite the signed URL's host or query.
3. `GET /uploads/{id}` returns `{parts_done: [{n, etag}], state, part_size,
   part_count, job_id}`. On refresh, ask the user to reselect the same file;
   verify its identity locally and send only missing parts. Browsers cannot
   silently reopen an arbitrary local file after refresh. The server cannot
   prove reselected file identity from the name and size alone.
4. `POST /uploads/{id}/complete` with `{parts: [{n, etag}]}` returns 202 with
   `{job_id}`. Poll the existing job endpoint. Repeating a successful completion
   returns the same ID. Submitted numbers must be contiguous; order is normalized.
5. `DELETE /uploads/{id}` returns 204 and releases storage. Repeating it is safe.
   Once completion starts, cancellation returns 409: retry completion instead.
   Once a job exists, use the existing job deletion API.

States: `open -> completing -> ready -> completed`; abort/cleanup uses
`aborting -> aborted`. `ready` means the job exists but queue acknowledgement
has not been recorded. Resume in `completing` or `ready` by repeating complete
with the same manifest, not by uploading more parts.

Errors use `{error: "readable message"}`. Request schema errors retain FastAPI's
422 `detail` response. 410 means an open session expired; start a new one.
415 rejects unsupported types or an unrecognized container; 422 rejects sizes,
part lists or ETags; 409 is a state conflict. On 503, refresh and retry; a response
failure does not prove that storage or queue publication failed.

## Consistency and safety

Postgres stores sessions separately from jobs. A per-session row lock serializes
completion, cancellation and cleanup. A frozen manifest allows completion to
recover after S3 merged an object but its response was lost. The unique object
key is never signed for whole-object writes. Both stored part sizes and final
object size are checked against the declaration. Only validated content creates
a Job, atomically with the session moving to `ready`.

New objects always use `application/octet-stream` and `Content-Disposition:
attachment`, never client-controlled MIME metadata. After the existing container
sniffer accepts the header, workers still perform actual decoding and set the
processed output's media type. Container sniffing alone does not prove decodability.

The job commits before enqueue. Redis and Postgres are not one transaction:
an ambiguous queue response can deliver the same job ID again. Existing worker
claims prevent duplicate processing; this does not claim exactly-once queue
delivery. The existing reaper recovers queued jobs missing from Redis. Pending
upload objects are protected from its orphan sweep.

The reaper runs upload cleanup on each pass. For a manual pass run
`python -m app.services.upload_cleanup` in the API environment.
It removes expired uncommitted uploads, retries failed cleanup and never
deletes a session's created job. S3/MinIO must also abort abandoned multipart
uploads after two days, covering an initiation whose storage response was lost.
Session rows remain as tombstones for idempotent responses; retention pruning
can be added separately.

## Local storage and deployment handoff

Apply `alembic upgrade head` before running the API or reaper. B adds migration
`20261008_05` after A's metadata migration; coordinate this shared schema addition.
Install updated Python dependencies (boto3 supplies public multipart APIs rather
than using private MinIO SDK methods). Rebuild API/worker images on deployment.

AWS Terraform already permits PUT, exposes ETag, grants multipart permissions,
and aborts abandoned parts after two days. No AWS provisioning change is required.

For local MinIO configure an `mc` alias yourself, then run
`sh deploy/configure-local-uploads.sh <alias>`. The script restarts that local
MinIO service; do not target production. CORS and stale multipart expiry are
configured server-wide. Confirm in browser developer tools that an OPTIONS request
permits PUT and the PUT response exposes ETag. This is storage CORS, separate
from FastAPI's CORS. The browser storage endpoint must be reachable and use the
page's TLS scheme. Keep service ports bound to loopback.

## Verification

Unit: `python -m pytest tests/test_upload_schema.py tests/test_multipart_storage.py
tests/test_multipart_uploads.py -q` (one command). Full suite: `pytest --cov=app`.
Integration tests require `RUN_POSTGRES_TESTS=1` and an isolated test database
and object store, with migrations applied.

Browser acceptance with E: upload a 1 GiB real video, inspect part progress,
interrupt and refresh, reselect the same file, resume missing parts, complete
and play the result. Also check expired signatures, cancellation, another user's
ID, renamed non-video input and the legacy upload UI. Record real results here;
unit mocks alone are not browser or production evidence.

## Implementation references

- [S3 multipart initiation](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/create_multipart_upload.html)
- [S3 multipart completion](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/complete_multipart_upload.html)
- [Community MinIO CORS and stale upload settings](https://github.com/minio/minio/blob/master/internal/config/api/api.go)
