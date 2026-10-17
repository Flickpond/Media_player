# Worker autoscaling (Track D)

Spot workers that scale with the queue, written and `terraform plan`ned in
sprint 4, **applied in sprint 5**. A root of its own that plugs into the
base in [`../terraform`](../terraform) through its outputs and never edits
its files (sprint 4 plan §2.3).

Sprint 5 moved Postgres to RDS and Redis to ElastiCache (Valkey), both
private and both TLS-only (contract 2.1). This root reads them the same way
the core host does:

| | How |
|---|---|
| Network | Egress to, and ingress on, the RDS and ElastiCache security groups (`db_security_group_id`, `cache_security_group_id`), never the core's |
| Addresses and credentials | `postgres_dsn`, `redis_host`, `redis_password` from `/flickpond/<env>/` in Parameter Store at boot, as `deploy/aws/fetch-secrets.sh` does on the core |
| TLS | `REDIS_SSL=true`; the DSN carries `?ssl=require` already |
| The core host | Not opened to the workers at all; `core_private_ip` is not needed |

```text
core host (base) ── queue-metrics ──▶ CloudWatch  Flickpond/QueuedJobs, RunningJobs
                                                    │ alarms
RDS Postgres 5432 ◀─┐  TLS, worker group only      ▼
ElastiCache 6379  ◀─┴── worker Auto Scaling Group  ◀── step scaling + scheduled minimum
                        spot, Graviton, 0..max_workers, one RQ worker each
```

| File | What |
|---|---|
| `compute.tf` | Launch template (AL2023 arm64, IMDSv2, encrypted gp3) and the group: 100% spot, `price-capacity-optimized`, five 4 vCPU instance types, capacity rebalance, a termination lifecycle hook for the drain. Two schedules: one warm worker 08:00–24:00 Asia/Shanghai, none overnight |
| `scaling.tf` | Out: jobs still queued at two consecutive one-minute samples → +1/+2/+4 by backlog. In: nothing queued or running for 15 minutes → back to the scheduled minimum. An alarm for when queue-depth data stops |
| `network.tf` | Worker security group: no inbound; out 443, and Postgres and Valkey to their own security groups. The matching ingress on those groups, behind `open_datastores_to_workers` (off) |
| `iam.tf` | One inline policy on the base's role: PutMetricData (namespace `Flickpond` only), pull from the worker repository, read `/flickpond/<env>/*` parameters, complete this group's lifecycle actions |
| `user-data.sh.tftpl` | Boot: Docker, the datastore settings from Parameter Store, ECR login, `python -m app.worker`, and the `flickpond-drain` service (contract 2.3) |
| `tests/` | `terraform test` with a mocked AWS provider: no account needed |

## Decisions, and why

- **A worker about to go gives its job back first (contract 2.3).** A
  systemd service on each instance polls its metadata every 5 s for a spot
  interruption notice or a `Terminated` target lifecycle state (scale-in,
  capacity rebalance), and answers both with `docker stop --time 100 worker`.
  The worker then stops FFmpeg and requeues its job (#73). A scale-in waits
  for that in the lifecycle hook (150 s, then CONTINUE); a spot reclaim does
  not wait for anything, so the 5 s poll plus 100 s fits its two minutes.
  An OS shutdown nobody warned about still gets `--stop-timeout 100`; a
  worker that dies with no warning at all is the reaper's (#74).
- **Scale out on "anything queued for two minutes".** RQ workers take a job
  the moment they are free, so a non-empty queue means every worker is busy.
- **Scale in only when everything is idle, straight to the minimum.** AWS
  picks which instance to terminate; scaling in while any job runs could
  pick the one encoding. A quarter-hour of idle spot costs cents; a lost 4K
  encode costs half an hour.
- **Step scaling, not target tracking.** "Backlog per worker" divides by zero
  overnight, and target tracking cannot scale out from an empty group.
- **Missing data does nothing; a separate alarm says so.** The publisher's
  healthcheck only pings Redis, so it can be "healthy" and publish nothing.
- **Schedules set `max_size` and `desired_capacity` to `-1`.** The provider
  defaults both to 0, so leaving them out would empty the group every night,
  mid-encode. A test pins it.
- **No secret and no address in Terraform.** The launch template holds the
  image and the parameter path; the DSN, the Valkey host and its auth token
  are fetched at boot, so they are in neither this root's state nor user
  data. A rotation in the base (`-replace=random_password.db`) reaches new
  workers with no change here.
- **Workers share the base's instance profile**, as agreed: they need its
  bucket access, and this root adds the rest to `app_role_name`.

## Run the tests (no AWS needed)

```bash
cd infra/workers
terraform init -backend=false
terraform test          # 11 runs, Terraform 1.10+
```

Without Terraform installed, prefix each command with
`docker run --rm -v "$PWD:/work" -w /work hashicorp/terraform:1.10`.

## Plan against staging

Needs AWS credentials, the base applied with the datastores (its state in
the S3 bucket from [`../bootstrap`](../bootstrap)), and an image in ECR.

```bash
cd infra/workers

# The base's outputs, under the same names (gitignored). Parameter names
# only; no secret is a Terraform output.
terraform -chdir=../terraform output -json \
  | jq '{vpc_id, subnet_ids, instance_profile_name, app_role_name,
         bucket_name, region, db_security_group_id, db_port,
         cache_security_group_id, cache_port} | map_values(.value)' \
  > base.auto.tfvars.json

cp example.backend.hcl staging.backend.hcl   # set the state bucket
cp example.tfvars workers.tfvars             # set worker_image
terraform init -backend-config=staging.backend.hcl
terraform plan -var-file=workers.tfvars -out=workers.plan
```

**Expected:** only additions, nothing changed or destroyed. With
`open_datastores_to_workers` off, one warning from
`check "workers_can_reach_the_datastores"`, and the only addition that
touches a base resource is `aws_iam_role_policy.workers` on its role. With
it on, two more: one ingress rule on the RDS group and one on the
ElastiCache group, each naming the worker group, never an address range.
Then confirm the base still sees no drift:

```bash
terraform -chdir=../terraform plan -var-file=staging.tfvars   # "No changes."
```

**Do not apply with the flag off.** Every worker would boot, fail to reach
the queue, and restart forever, on a spot instance billed by the second.

## Before the first apply

1. The base applied with RDS and ElastiCache, and the three parameters it
   writes present under `/flickpond/<env>/`: `postgres_dsn`, `redis_host`,
   `redis_password`. This root creates none of them.
2. The plan with `open_datastores_to_workers = true` read by someone other
   than whoever applies it ([`docs/datastore-review.md`](../../docs/datastore-review.md),
   sprint 5 rule 7). The reviewer's checklist is
   [`docs/d-worker-datastore-review.md`](../../docs/d-worker-datastore-review.md).
   After the apply, repeat that note's reachability test:
   both datastores still unreachable from outside AWS.
3. The queue-metrics publisher running on the core host with
   `METRICS_ENVIRONMENT` equal to `environment` here, and reaching
   ElastiCache over TLS like the API (`REDIS_SSL=true` in its `.env`).
4. Giving a job back on SIGTERM (#73) on the `main` commit the image is
   built from: the drain only sends the signal.
