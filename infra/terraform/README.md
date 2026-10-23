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
| `cdn.tf` | CloudFront for HLS segments behind signed URLs; with `cdn_alias`, on our own name and certificate with TLS 1.2 enforced (below) |
| `tests/` | `terraform test` with mocked providers, no account needed: `terraform init -backend=false && terraform test` |
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

## The CDN on our own name (sprint 6, Track D)

`*.cloudfront.net` shares AWS's certificate, on which CloudFront still accepts
TLS 1.0 whatever the distribution asks for (#88). With `cdn_alias` set, the
distribution answers to `cdn.flickpond.com` on its own ACM certificate
(us-east-1, as CloudFront requires) with `TLSv1.2_2021` as the floor, and the
API signs segment URLs for that name. Null — the default — changes nothing:
**a plan without it must say "No changes"**, or stop.

The DNS is at Spaceship, not Route 53, so it takes two applies (`...` is the
`docker run ... hashicorp/terraform:1.10` prefix from "Apply" above):

```powershell
# 1. Ask for the certificate only, and read the record ACM wants.
... plan "-var-file=staging.tfvars" "-var=cdn_alias=cdn.flickpond.com" "-target=aws_acm_certificate.cdn" "-out=cert.plan"
... apply cert.plan
... output cdn_certificate_validation   # name, type CNAME, value
... output cdn_domain                   # d…cloudfront.net
```

2. At Spaceship add **both** CNAMEs: the validation record above, and
   `cdn` → the `cdn_domain` value. Leave them in place for good: ACM renews
   against the same record.
3. Put `cdn_alias = "cdn.flickpond.com"` in `staging.tfvars`, then plan and
   apply in full. The plan should change only the distribution (in place),
   the `cloudfront_domain` parameter, and add the validation; the apply
   waits until ACM has issued the certificate.
4. Prove it before the API uses it — a TLS 1.1 handshake must fail and 1.2
   succeed, and an unsigned request must get 403 from CloudFront, not a
   certificate error:
   ```bash
   openssl s_client -connect cdn.flickpond.com:443 -servername cdn.flickpond.com -tls1_1 </dev/null   # handshake failure
   openssl s_client -connect cdn.flickpond.com:443 -servername cdn.flickpond.com -tls1_2 </dev/null   # Verify return code: 0
   curl -sS -o /dev/null -w '%{http_code}\n' https://cdn.flickpond.com/outputs/x                     # 403
   ```
5. On the core: `sudo bash deploy/aws/fetch-secrets.sh staging`, then restart
   the API (`... up -d api`). Play a video and check in the browser that
   segments come from `cdn.flickpond.com`.

**Back out** in the opposite order, or playback breaks in between: first set
`CLOUDFRONT_DOMAIN` in the core's `.env` to the `cdn_domain` value and
restart the API, then remove `cdn_alias` and apply.

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
