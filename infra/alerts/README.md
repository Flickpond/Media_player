# Alerts that reach someone (Track D, sprint 6)

Before this root, the queue-metrics alarm had no action: it turned red in a
console nobody was looking at (sprint 6 plan, §5). This root gives every
alert the same destination — email — and adds the two that were missing:

| Alert | Source | Fires when | Topic |
|---|---|---|---|
| **Site down** | Route 53 health check on `https://flickpond.com/api/health` from three regions, every 30 s | The API's own answer (`"status":"ok"`) is missing for about 3½ minutes; again when it is back | `flickpond-<env>-site-health` (**us-east-1**) |
| **Queue metrics missing** | `flickpond-<env>-queue-metrics-missing` in [`../workers`](../workers) | No queue-depth datapoint for 5 minutes: autoscaling is blind | `flickpond-<env>-alerts` |
| **Budget** | AWS Budgets, the whole account | This month's cost passes US$80, or AWS forecasts it will | `flickpond-<env>-alerts` |

| File | What |
|---|---|
| `topics.tf` | The two topics and an email subscription per address on each. The main topic's policy admits CloudWatch and Budgets, for this account only |
| `health.tf` | The health check (HTTPS, SNI, body must match) and its alarm in us-east-1 |
| `budget.tf` | The monthly cost budget: actual and forecast, both over 100% |
| `tests/` | `terraform test` with mocked providers: no account needed |

## Decisions, and why

- **Two topics.** Route 53 publishes health-check metrics only to us-east-1,
  and a CloudWatch alarm can only notify a topic in its own region. So each
  address confirms twice, once per topic.
- **The body must match, not just the status.** An error page nginx serves
  with 200 is not the API answering.
- **Missing data is an outage.** A health check that stopped reporting must
  not look like a healthy site (`treat_missing_data = "breaching"`).
- **A deploy should not page.** Down after three failed checks in a row
  (90 s), then two minutes of that: an API restart is well inside it.
- **The whole account, not a tag.** A tag filter only sees what someone
  remembered to tag, and the surprise bill is the thing nobody tagged.
- **No encryption on the topics.** CloudWatch and Budgets cannot publish to a
  topic under the AWS-managed SNS key, and an alert holds no secret.
- **The addresses are not in git.** They live in the gitignored
  `alerts.tfvars`, and in the state, which is private.

## Run the tests (no AWS needed)

```bash
cd infra/alerts
terraform init -backend=false
terraform test          # 7 runs, Terraform 1.10+
```

Without Terraform installed, prefix each command with
`docker run --rm -v "$PWD:/work" -w /work hashicorp/terraform:1.10`.

## Apply

Needs credentials that may manage SNS, CloudWatch, Route 53 health checks and
Budgets. Nothing here touches the base, the workers, or a datastore.

```bash
cd infra/alerts
cp example.backend.hcl staging.backend.hcl   # set the state bucket
cp example.tfvars alerts.tfvars              # set alert_emails
terraform init -backend-config=staging.backend.hcl
terraform plan -var-file=alerts.tfvars -out=alerts.plan
#   Expected: only additions -- 2 topics, 1 topic policy, 2 subscriptions per
#   address, 1 health check, 1 alarm, 1 budget. Nothing changed or destroyed.
terraform apply alerts.plan
```

**Then everyone on the list clicks both confirmation links** (subjects
"AWS Notification - Subscription Confirmation"; check spam). Until then the
subscriptions are `PendingConfirmation` and deliver nothing:

```bash
aws sns list-subscriptions-by-topic --topic-arn "$(terraform output -raw alerts_topic_arn)" \
  --query 'Subscriptions[].[Endpoint,SubscriptionArn]' --output table
aws sns list-subscriptions-by-topic --region us-east-1 \
  --topic-arn "$(terraform output -raw site_health_topic_arn)" \
  --query 'Subscriptions[].[Endpoint,SubscriptionArn]' --output table
```

Finally, connect the queue-metrics alarm. In `infra/workers`, set
`alarm_topic_arn` in `workers.tfvars` to `terraform output -raw alerts_topic_arn`
and plan with **the tfvars the group was last applied with**. The only
change should be `aws_cloudwatch_metric_alarm.metrics_missing`, updated in
place. Anything else — the launch template above all — means the tfvars are
not the applied ones: stop.

## Prove each alert arrives

A forced state lasts until the next evaluation (about a minute), so each test
sends an ALARM email and then an OK email.

```bash
# Site down (us-east-1)
aws cloudwatch set-alarm-state --region us-east-1 \
  --alarm-name flickpond-staging-site-down \
  --state-value ALARM --state-reason "Delivery test, not an outage"

# Queue metrics missing
aws cloudwatch set-alarm-state --region ap-southeast-1 \
  --alarm-name flickpond-staging-queue-metrics-missing \
  --state-value ALARM --state-reason "Delivery test, not an outage"

# The health check itself: every checker should say Success
aws route53 get-health-check-status --health-check-id "$(terraform output -raw health_check_id)" \
  --query 'HealthCheckObservations[].[Region,StatusReport.Status]' --output table
```

Budgets cannot be forced. To see a real one arrive, apply with
`-var monthly_budget_usd=1` (the month's spend is already over it), wait for
the email — Budgets refreshes a few times a day — then apply again at 80.

## Turn it off

`terraform destroy -var-file=alerts.tfvars`, after setting `alarm_topic_arn`
back to null in `infra/workers` and applying that first: an alarm pointing
at a deleted topic fails every notification silently.

## Cost, roughly

A Route 53 health check on an AWS endpoint, with HTTPS and string matching,
is a few US dollars a month; one CloudWatch alarm is about US$0.10 a month;
SNS email at this volume and the account's first budgets cost nothing. Check
the AWS pricing pages before relying on these.
