# AWS base (sprint 4, Track A)

One EC2 host running the Compose stack, with S3 in place of MinIO. This is
the **base** from the sprint 4 plan §2.3: D's autoscaling module (sprint 5)
plugs into it through [`outputs.tf`](outputs.tf) and does not edit these
files.

| File | What |
|---|---|
| `s3.tf` | Private bucket, encrypted. CORS for direct upload (PUT, `ETag` exposed) and hls.js (GET). Abandoned multipart uploads expire after 2 days |
| `iam.tf` | Instance role: this bucket only, plus Session Manager. No access keys anywhere |
| `compute.tf` | Default VPC, security group (80/443; 22 only from `admin_cidrs`), Graviton instance with IMDSv2, Elastic IP |
| `outputs.tf` | The interface D builds on — change it only after telling D |

## Apply

Needs AWS credentials for the account (`aws configure` or `AWS_PROFILE`).

```bash
cd infra/terraform
cp example.tfvars staging.tfvars        # gitignored; set your IP and origin
terraform init
terraform plan  -var-file=staging.tfvars -out=staging.plan
terraform apply staging.plan
terraform output                         # bucket, region, IP for .env and DNS
```

Without Terraform installed:

```bash
docker run --rm -it -v "$PWD:/work" -w /work \
  -v "$HOME/.aws:/root/.aws:ro" hashicorp/terraform:1.9 plan -var-file=staging.tfvars
```

## What it deliberately does not do

- **Deploy the app.** User data installs Docker, Compose and git; the deploy
  is the same `git` + `docker compose up -d --build --scale worker=2` as on
  the current server. Doing it in user data would mean baking repo
  credentials into the instance.
- **Remote state.** Local state is fine for one operator. It moves to an S3
  backend with locking before D's module is ever applied (sprint 5).
- **A dedicated VPC.** The default VPC has public subnets and no NAT gateway
  to pay for. Sprint 5 needs a private network for Postgres and Redis once
  workers run on other hosts; that is when it is built.

## Cost, roughly (ap-southeast-1, on demand)

`c7g.2xlarge` is roughly US$0.30–0.35/h — under US$10 a day if left
running, so **stop it when not testing** (`aws ec2 stop-instances`; the
Elastic IP keeps the address, at a small hourly charge). 50 GB of gp3 is a
few dollars a month. S3 storage and transfer depend on use. Check the AWS
pricing page before relying on these.
