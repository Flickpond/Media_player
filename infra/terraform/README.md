# AWS base (Track A, sprints 4 and 5)

The core host, S3 in place of MinIO, and — since sprint 5 — PostgreSQL and
the job queue as managed services. This is the **base**: D's autoscaling
module (`infra/workers`) plugs into it through [`outputs.tf`](outputs.tf) and
does not edit these files.

| File | What |
|---|---|
| `s3.tf` | Private bucket, encrypted. CORS for direct upload (PUT, `ETag` exposed) and hls.js (GET). Abandoned multipart uploads expire after 2 days |
| `iam.tf` | Instance role: this bucket, this environment's SSM secrets, Session Manager. No access keys anywhere |
| `compute.tf` | Default VPC, security group (80/443; 22 only from `admin_cidrs`), Graviton instance with IMDSv2, Elastic IP |
| `datastores.tf` | **RDS PostgreSQL** (TLS forced, private, 7 days of backups) and **ElastiCache Valkey** (TLS, auth token, never public), each admitting only the core; their passwords generated here and kept in SSM under `/flickpond/<env>/` |
| `outputs.tf` | The interface D builds on (contract 2.1 of the sprint 5 plan) — change it only after telling D |

**Read [`docs/datastore-review.md`](../../docs/datastore-review.md) before the
first apply that creates the datastores**, and have someone else read the plan.

## First time: remote state

State holds the generated passwords, so it lives in a private, encrypted,
versioned bucket, locked while anyone applies. Once per AWS account:

```powershell
cd infra/bootstrap
docker run --rm -it -v "${PWD}:/work" -w /work -v "$HOME\.aws:/root/.aws:ro" hashicorp/terraform:1.10 init
docker run --rm -it -v "${PWD}:/work" -w /work -v "$HOME\.aws:/root/.aws:ro" hashicorp/terraform:1.10 apply
docker run --rm -v "${PWD}:/work" -w /work hashicorp/terraform:1.10 output -raw state_bucket
```

Then move this root's existing local state into it:

```powershell
cd ..\terraform
copy example.backend.hcl staging.backend.hcl     # gitignored; put the bucket name in
docker run --rm -it -v "${PWD}:/work" -w /work -v "$HOME\.aws:/root/.aws:ro" hashicorp/terraform:1.10 init "-backend-config=staging.backend.hcl" -migrate-state
```

Answer `yes` to copy the state. After that the local `terraform.tfstate` can
be deleted.

## Apply

```powershell
docker run --rm -it -v "${PWD}:/work" -w /work -v "$HOME\.aws:/root/.aws:ro" hashicorp/terraform:1.10 plan "-var-file=staging.tfvars" "-out=staging.plan"
docker run --rm -it -v "${PWD}:/work" -w /work -v "$HOME\.aws:/root/.aws:ro" hashicorp/terraform:1.10 apply staging.plan
docker run --rm -v "${PWD}:/work" -w /work hashicorp/terraform:1.10 output
```

PowerShell needs the `"-var-file=..."` quotes: unquoted, it splits arguments
at the dot.

Reading a secret on a host (never print it into a shared terminal):

```bash
aws ssm get-parameter --with-decryption --name /flickpond/staging/postgres_dsn --query Parameter.Value --output text
```

## What it deliberately does not do

- **Deploy the app.** User data installs Docker, Compose and git; the deploy
  is the runbook in [`docs/aws-cutover.md`](../../docs/aws-cutover.md).
  Doing it in user data would mean baking repo credentials into the instance.
- **A dedicated VPC.** The default VPC has subnets in every zone and no NAT
  gateway to pay for. The datastores sit in it with no public address, and
  their security groups admit only the core and the workers.
- **Highly available datastores.** One RDS instance and one Valkey node.
  Backups restore the database; the queue is rebuilt from Postgres by the
  reaper if it is ever lost.

## Cost, roughly (ap-southeast-1, on demand)

`c7g.2xlarge` is roughly US$0.30–0.35/h — under US$10 a day if left
running, so **stop it when not testing** (`aws ec2 stop-instances`; the
Elastic IP keeps the address, at a small hourly charge). 50 GB of gp3 is a
few dollars a month. S3 storage and transfer depend on use. Check the AWS
pricing page before relying on these.
