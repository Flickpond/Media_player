# Track D, sprint 5 — AWS steps for someone with credentials

**For:** Ibrahim (Track A), or whoever holds AWS credentials for the account.
**From:** Jiang Yibai (Track D), who has none. Every step below is
copy-paste; the only value not known from the repo is the account ID.
**Why:** sprint 5 plan §4, Monday for D: "Apply `infra/registry`; set the
repository variables so CI pushes a worker image per merge." Checkpoint for
Monday: *ECR holds a worker image built from `main`.*

## Where things stand (17 October)

- `infra/registry` has never been applied: no local state exists and the
  **Worker image** workflow's last run (#16, `735da33`) logged
  `ROLE_ARN`, `REGION` and `ECR_REPOSITORY` as empty, then
  *"Worker image not pushed: ECR is not configured."* The push job was
  skipped, so the green tick does not mean an image exists.
- `infra/registry` and `infra/workers` now keep their state in the base's
  S3 bucket with a lock file, like `infra/terraform`. Terraform **1.10 or
  newer** is required.
- `infra/workers` now reads contract 2.1: it adds rules to the RDS and
  ElastiCache security groups, and its workers read `postgres_dsn`,
  `redis_host` and `redis_password` from `/flickpond/<env>/` at boot over
  TLS. It no longer needs `core_private_ip`.

## Values

`<ACCOUNT_ID>` is the 12-digit account, from
`aws sts get-caller-identity --query Account --output text`.

| What | Value |
|---|---|
| Region | `ap-southeast-1` |
| State bucket | `flickpond-tfstate-<ACCOUNT_ID>` (created by `infra/bootstrap`) |
| Registry state key | `registry/terraform.tfstate` |
| ECR repository | `flickpond/worker` |
| Repository URL | `<ACCOUNT_ID>.dkr.ecr.ap-southeast-1.amazonaws.com/flickpond/worker` |
| CI push role | `arn:aws:iam::<ACCOUNT_ID>:role/flickpond-ci-ecr-push` |
| Role trusts | `repo:Flickpond/Media_player:ref:refs/heads/main` only |

GitHub repository **variables** (Settings → Secrets and variables →
Actions → **Variables** tab, not Secrets — none of these is secret):

| Name | Value |
|---|---|
| `AWS_ECR_PUSH_ROLE_ARN` | `arn:aws:iam::<ACCOUNT_ID>:role/flickpond-ci-ecr-push` |
| `AWS_REGION` | `ap-southeast-1` |
| `ECR_REPOSITORY` | `flickpond/worker` |

Prefer copying the three from `terraform output` (step 1) over typing them.

## 1. Apply `infra/registry`

Needs: IAM (create an OIDC provider, a role and its inline policy), ECR
(create a repository and its lifecycle policy), and read/write on the state
bucket.

```bash
cd infra/registry

# Exactly one GitHub OIDC provider may exist per account.
aws iam list-open-id-connect-providers
#   lists ...oidc-provider/token.actions.githubusercontent.com?
#   yes -> add  -var create_github_oidc_provider=false  to the plan below
#   no  -> leave it out

cp example.backend.hcl registry.backend.hcl     # gitignored
# edit registry.backend.hcl: bucket = "flickpond-tfstate-<ACCOUNT_ID>"

terraform init -backend-config=registry.backend.hcl
terraform plan -out=registry.plan
terraform apply registry.plan

terraform output -raw ci_push_role_arn   # -> AWS_ECR_PUSH_ROLE_ARN
terraform output -raw region             # -> AWS_REGION
terraform output -raw repository_name    # -> ECR_REPOSITORY
terraform output -raw repository_url     # -> worker_image, step 4
```

**Expected plan:** additions only — the ECR repository and its lifecycle
policy, the role `flickpond-ci-ecr-push` and its inline policy, and the OIDC
provider unless it already existed. Nothing here touches the base, a
security group or a datastore, so rule 7's review does not apply.

## 2. Set the three repository variables

Settings → Secrets and variables → Actions → Variables → **New repository
variable**, three times, with the values from step 1. Or, with the GitHub
CLI and admin rights on the repository:

```bash
gh variable set AWS_ECR_PUSH_ROLE_ARN --repo Flickpond/Media_player --body "$(terraform output -raw ci_push_role_arn)"
gh variable set AWS_REGION            --repo Flickpond/Media_player --body "$(terraform output -raw region)"
gh variable set ECR_REPOSITORY        --repo Flickpond/Media_player --body "$(terraform output -raw repository_name)"
```

## 3. Push the first image

Actions → **Worker image** → *Run workflow* → branch **`main`** (the role
refuses any other ref). After this, every push to `main` that passes CI
pushes its own image without anyone doing anything.

In the run:

- *Decide whether to push* has no "not pushed" notice.
- *Build, scan and push (linux/arm64)* runs. Its summary shows the commit,
  image and digest.
- If that job sits in **Queued**, the `ubuntu-24.04-arm` runner is not
  available to this repository; tell D.
- If **Scan** fails, Trivy found a fixable CRITICAL or HIGH in the arm64
  image; nothing was pushed. Its table names the package.

Prove the checkpoint by asking ECR, not the workflow:

```bash
aws ecr describe-images --region ap-southeast-1 --repository-name flickpond/worker \
  --query 'imageDetails[].[imageTags[0],imagePushedAt]' --output table
```

One row tagged with the full 40-character SHA of `main`.

## 4. For D's `terraform plan` of the worker group (Tuesday)

Only once the base on staging has RDS and ElastiCache applied.

```bash
# The three parameters the workers read at boot. Names only, never values.
aws ssm get-parameters-by-path --region ap-southeast-1 --path /flickpond/staging \
  --query 'Parameters[].Name' --output text
#   must include postgres_dsn, redis_host and redis_password

cd infra/workers
terraform -chdir=../terraform output -json \
  | jq '{vpc_id, subnet_ids, instance_profile_name, app_role_name,
         bucket_name, region, db_security_group_id, db_port,
         cache_security_group_id, cache_port} | map_values(.value)' \
  > base.auto.tfvars.json

cp example.backend.hcl staging.backend.hcl   # bucket = "flickpond-tfstate-<ACCOUNT_ID>"
cp example.tfvars workers.tfvars
# workers.tfvars:
#   worker_image = "<repository_url>:<the 40-character SHA from step 3>"
#   keep open_datastores_to_workers commented out for the first plan

terraform init -backend-config=staging.backend.hcl
terraform plan -var-file=workers.tfvars -out=workers.plan
terraform show -no-color workers.plan > plan.txt   # gitignored; share this
```

**Expected:** additions only, one warning from
`check "workers_can_reach_the_datastores"`, and nothing changed in the base
(`terraform -chdir=../terraform plan -var-file=staging.tfvars` says
*No changes*). Then the same with `open_datastores_to_workers = true`: two
more rules, one on `flickpond-staging-db` and one on
`flickpond-staging-cache`, each naming the worker security group and no
`cidr_ipv4`. **Rule 7:** whoever applies that is not whoever reviews it —
if A applies, D reads `plan.txt` first; and the other way round.

## Asks for Track A

1. **`redis_host` is not in contract 2.1's list** but the workers read it,
   as `deploy/aws/fetch-secrets.sh` does on the core. Please keep the name.
2. `docs/aws-cutover.md` §2 lists `core_private_ip` among the outputs D
   consumes; the worker module no longer reads it. Yours to keep or drop.

## Record here

| Step | Done by | Date | Result |
|---|---|---|---|
| 1. Registry applied (OIDC provider created or reused?) | | | |
| 2. Three variables set | | | |
| 3. First image in ECR (tag) | | | |
| 4. Parameters present on staging | | | |
