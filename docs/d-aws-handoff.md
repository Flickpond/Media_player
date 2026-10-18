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
if A applies, D reads `plan.txt` first; and the other way round. The
reviewer's checklist, and what to test after the apply, is
[`d-worker-datastore-review.md`](d-worker-datastore-review.md).

## 5. Apply the worker group on staging (Wednesday)

Only after the plan with `open_datastores_to_workers = true` has been read
by someone other than whoever applies it. Apply from `main` once D's
`infra/workers` PR has merged, with `worker_image` pointing at a commit that
includes #73 (every image CI pushes from now on does).

```bash
cd infra/workers
terraform plan -var-file=workers.tfvars -out=workers.plan   # flag on; reviewed
terraform apply workers.plan
terraform output     # autoscaling_group_name, worker_security_group_id, ...
```

Then prove Wednesday's checkpoint, one line at a time
(`G=flickpond-staging-workers`):

```bash
# The hook and the two schedules exist.
aws autoscaling describe-lifecycle-hooks --auto-scaling-group-name $G \
  --query 'LifecycleHooks[].[LifecycleHookName,LifecycleTransition,HeartbeatTimeout,DefaultResult]'
#   drain-worker  autoscaling:EC2_INSTANCE_TERMINATING  150  CONTINUE
aws autoscaling describe-scheduled-actions --auto-scaling-group-name $G \
  --query 'ScheduledUpdateGroupActions[].[ScheduledActionName,Recurrence,TimeZone,MinSize]'

# One worker now, without waiting for 08:00. It is spot.
aws autoscaling set-desired-capacity --auto-scaling-group-name $G --desired-capacity 1
aws ec2 describe-instances --filters "Name=tag:aws:autoscaling:groupName,Values=$G" \
  "Name=instance-state-name,Values=pending,running" \
  --query 'Reservations[].Instances[].[InstanceId,InstanceLifecycle,InstanceType]' --output table
#   InstanceLifecycle = spot

# On it: the worker is listening and the drain is watching.
aws ssm start-session --target <instance id>
sudo docker logs worker 2>&1 | tail          # "worker starting", queues video_jobs, video_jobs-ladder
systemctl is-active flickpond-drain          # active
sudo tail -50 /var/log/cloud-init-output.log # only if either of those is wrong
```

1. **A spot worker takes a job:** upload a video on staging; it reaches
   `done`, and `docker logs worker` on the instance shows it.
2. **Queue-depth scaling:** upload four or five videos at once. Within about
   three minutes `flickpond-staging-jobs-waiting` goes to ALARM and the
   group's desired capacity rises
   (`aws cloudwatch describe-alarms --alarm-name-prefix flickpond-staging --query 'MetricAlarms[].[AlarmName,StateValue]'`).
   Fifteen idle minutes after the last job, it returns to the minimum.
3. **`docker stop` mid-encode gives the job back and another worker finishes
   it:** with two workers up (`--desired-capacity 2`) and a long upload
   encoding, on the instance running it:
   `sudo docker stop --time 100 worker`. The job goes back to `queued`
   within seconds, the other worker takes it, and it ends `done`.
   `sudo docker start worker` afterwards.
4. **A scale-in drains:** while a job encodes,
   `aws autoscaling terminate-instance-in-auto-scaling-group --instance-id <id> --should-decrement-desired-capacity`.
   On that instance `journalctl -u flickpond-drain` shows
   `scale-in: stopping the worker…`, the job is requeued, and the instance
   terminates within seconds of that rather than after the hook's 150 s.
   (The spot path — an interruption notice — is Thursday's FIS test.)

**Undo:** `terraform destroy -var-file=workers.tfvars` removes every resource
this root made, including the two datastore rules and the policy on the
base's role; the base is untouched. That policy is also what lets the
core's queue-metrics publisher write to CloudWatch, so its metric stops too.

## 6. Thursday: interruption test, login numbers, DAST on staging

### 6.1 A real spot interruption (with A)

`fis.tf` holds an AWS FIS experiment that sends one running worker of this
group the same two-minute notice a reclaim does. Off by default; on for the
test only:

```bash
cd infra/workers
terraform plan -var-file=workers.tfvars -var interruption_test=true -out=fis.plan
#   3 to add: the experiment's role, its policy, the template. Nothing else.
terraform apply fis.plan

# One worker, so it is the one the experiment picks.
aws autoscaling set-desired-capacity --auto-scaling-group-name $G --desired-capacity 1
```

Upload a long video (A's 4K phone clip) and wait for `processing`. On the
worker: `aws ssm start-session --target <id>`, then
`sudo journalctl -u flickpond-drain -f`. From your machine:

```bash
aws fis start-experiment \
  --experiment-template-id "$(terraform output -raw interruption_test_template_id)"
```

**Expected:** within 5 s the journal shows
`spot interruption notice: stopping the worker so it gives its job back`;
the job returns to `queued` within seconds (its first of three
interruptions); about two minutes later the instance is terminated; the
group launches a replacement, which takes the job and finishes it `done`.
Then remove the test's resources:

```bash
terraform plan -var-file=workers.tfvars -out=workers.plan   # 3 to destroy, nothing else
terraform apply workers.plan
```

### 6.2 Login latency on staging (§5 of `login-latency-investigation.md`)

The fix (bcrypt off the event loop) is in D's Thursday PR. Measure staging
**before** that PR is deployed there and again **after**, with the two
commands in §5 of that document, and record both there.

### 6.3 DAST against staging

1. Repository variable **`DAST_STAGING_URL`** = the staging origin, e.g.
   `https://staging.example.com` — `https://`, no path, no trailing route.
   The workflow refuses anything else, and refuses `flickpond.com` and
   `www.flickpond.com` outright.
2. Actions → **DAST** → *Run workflow* → branch `main` → target **staging**.
3. The run's summary has the findings table; the full report is the
   `zap-reports-staging-<run id>` artifact. Medium or High findings not in
   `deploy/dast/accepted-alerts.tsv` fail the run, as on pull requests.
4. The scan registers `dast-<run id>@example.com` on staging and fuzzes the
   API as that user. Delete the account with the operator's
   `DELETE /admin/users/{id}` once it lands (contract 2.4).

## Asks for Track A

1. **`redis_host` is not in contract 2.1's list** but the workers read it,
   as `deploy/aws/fetch-secrets.sh` does on the core. Please keep the name.
2. `docs/aws-cutover.md` §2 lists `core_private_ip` among the outputs D
   consumes; the worker module no longer reads it. Yours to keep or drop.
3. **D's Thursday PR edits your `app/api/auth.py`** (rule 5): the login fix
   from `login-latency-investigation.md` §4 — `hash_password` and
   `verify_password` through `run_in_threadpool`, nothing else changed —
   with a regression test in `tests/test_auth.py`. Please review it.

## Record here

| Step | Done by | Date | Result |
|---|---|---|---|
| 1. Registry applied (OIDC provider created or reused?) | | | |
| 2. Three variables set | | | |
| 3. First image in ECR (tag) | | | |
| 4. Parameters present on staging | | | |
| 5. Worker group applied (reviewed by) | | | |
| 5.1 Spot worker took a job | | | |
| 5.2 Scaled out on queue depth, back to minimum | | | |
| 5.3 `docker stop` mid-encode → requeued → done elsewhere | | | |
| 5.4 Scale-in drained the job | | | |
| 6.1 FIS notice → drained → requeued → done on the replacement | | | |
| 6.2 Login p90 on staging, before / after the fix | | | |
| 6.3 DAST on staging (run id, blocking findings) | | | |
