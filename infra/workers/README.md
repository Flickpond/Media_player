# Worker autoscaling (sprint 4 groundwork, Track D)

Spot workers that scale with the queue, written and `terraform plan`ned in
sprint 4, **applied in sprint 5**. A root of its own that plugs into the
base in [`../terraform`](../terraform) through its outputs and never edits
its files (sprint 4 plan §2.3).

```text
core host (base) ── queue-metrics ──▶ CloudWatch  Flickpond/QueuedJobs, RunningJobs
     ▲  Postgres 5432, Redis 6379                   │ alarms
     │  (worker security group only, sprint 5)       ▼
  worker Auto Scaling Group  ◀──────────── step scaling + scheduled minimum
  spot, Graviton, 0..max_workers, one RQ worker each
```

| File | What |
|---|---|
| `compute.tf` | Launch template (AL2023 arm64, IMDSv2, encrypted gp3) and the group: 100% spot, `price-capacity-optimized`, five instance types, capacity rebalance. Two schedules: one warm worker 08:00–24:00 Asia/Shanghai, none overnight |
| `scaling.tf` | Out: jobs still queued at two consecutive one-minute samples → +1/+2/+4 by backlog. In: nothing queued or running for 15 minutes → back to the scheduled minimum. An alarm for when queue-depth data stops |
| `network.tf` | Worker security group: no inbound; out 443 and Postgres/Redis on the core host. The matching ingress on the base's group, behind `open_datastores_to_workers` (off) |
| `iam.tf` | One inline policy on the base's role: PutMetricData (namespace `Flickpond` only), pull from the worker repository, read `/flickpond/<env>/*` parameters |
| `user-data.sh.tftpl` | Boot: Docker, the two passwords from Parameter Store, ECR login, `python -m app.worker` |
| `tests/` | `terraform test` with a mocked AWS provider: no account needed |

## Decisions, and why

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
- **No secret in Terraform.** The launch template holds the image and the
  core host's address; the passwords are SecureStrings fetched at boot, so
  they are in neither state nor user data.
- **Workers share the base's instance profile**, as agreed: they need its
  bucket access, and this root adds the rest to `app_role_name`.

## Run the tests (no AWS needed)

```bash
cd infra/workers
terraform init -backend=false
terraform test          # 9 runs, Terraform 1.7+
```

Without Terraform installed, prefix each command with
`docker run --rm -v "$PWD:/work" -w /work hashicorp/terraform:1.9`.

## Plan against staging (sprint 4 checkpoint)

Needs AWS credentials, the base applied, and its state on this machine.

```bash
cd infra/workers

# The base's seven outputs, under the same names (gitignored).
terraform -chdir=../terraform output -json \
  | jq '{vpc_id, subnet_ids, security_group_id, instance_profile_name,
         app_role_name, bucket_name, region} | map_values(.value)' \
  > base.auto.tfvars.json

cp example.tfvars workers.tfvars   # set core_private_ip and worker_image
terraform init
terraform plan -var-file=workers.tfvars -out=workers.plan
```

**Expected:** only additions, nothing changed or destroyed, and one warning
from `check "workers_can_reach_the_datastores"`. The only addition that
touches a base resource is `aws_iam_role_policy.workers` on its role.
Then confirm the base still sees no drift:

```bash
terraform -chdir=../terraform plan -var-file=staging.tfvars   # "No changes."
```

**Do not apply in sprint 4.** With the datastores closed, every worker
would boot, fail to reach Redis, and restart forever, on a spot instance
billed by the second.

## Before the first apply (sprint 5)

1. State for both roots on the S3 backend with locking.
2. The two passwords, as SecureStrings, equal to the core host's `.env`:
   `aws ssm put-parameter --type SecureString --name /flickpond/<env>/redis-password --value …`
   (and `postgres-password`).
3. Postgres and Redis on the core host bound to its private address as well
   as loopback (a compose change, and an exception to the sprint 1 rule that
   needs its own write-up), then `open_datastores_to_workers = true`.
4. The queue-metrics publisher running on the core host with
   `METRICS_ENVIRONMENT` equal to `environment` here.
5. A worker that re-queues its job on SIGTERM, or a shorter reaper lease:
   a spot reclaim gives two minutes, and today a cut-off job waits for the
   40-minute lease.
