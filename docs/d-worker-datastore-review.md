# Worker group review — before `infra/workers` opens the datastores

**For:** whoever reviews the `infra/workers` plan with
`open_datastores_to_workers = true` (sprint 5 rule 7: not the person who
applies it). The datastores themselves are covered by A's
[`datastore-review.md`](datastore-review.md); this is the other half, what
the worker group adds to them.
**Written:** Tuesday 13 (D), against an offline plan with the AWS provider
mocked. Repeat the checks on the real staging plan before Wednesday's apply.

## What opens

| | Rule | From / to |
|---|---|---|
| RDS group (`flickpond-<env>-db`) | **ingress** tcp `db_port` (5432) | the worker security group only |
| ElastiCache group (`flickpond-<env>-cache`) | **ingress** tcp `cache_port` (6379) | the worker security group only |
| Worker group (`flickpond-<env>-worker`) | egress tcp 5432 / 6379 | the RDS / ElastiCache group |
| Worker group | egress tcp 443 | `0.0.0.0/0`: ECR, S3, SSM, package mirrors |
| Worker group | **no ingress at all** | — |

Nothing on the core host's group changes. Workers have a public address
(default VPC, no NAT gateway) but no inbound rule, so nothing can connect
to them.

**Credentials:** none in Terraform, the launch template or user data.
At boot the instance role reads `postgres_dsn`, `redis_host` and
`redis_password` from `/flickpond/<env>/` into a root-only env file
(`/etc/flickpond/worker.env`, mode 600). **In transit:** `REDIS_SSL=true`;
the DSN carries `?ssl=require` and RDS refuses plain text anyway.
**Role:** workers share the core's instance profile, so a compromised
worker holds what the core holds — every video and this environment's
secrets. IMDSv2 is required; hop limit 2 so the container can use the role.

## What the reviewer checks in the plan

Ask for `terraform show -no-color workers.plan`, and the base's plan.

- [ ] `Plan: 16 to add, 0 to change, 0 to destroy.` (14 with the flag off.)
- [ ] The two `datastores_from_workers` rules: `security_group_id` equals the
      base's `db_security_group_id` / `cache_security_group_id`;
      `referenced_security_group_id` is the new worker group; **no
      `cidr_ipv4` or `cidr_ipv6` line at all.**
- [ ] `0.0.0.0/0` appears exactly once: `egress_rule.https`, port 443.
- [ ] `aws_iam_role_policy.workers`: SSM read scoped to
      `parameter/flickpond/<env>/*`; ECR pull scoped to the one
      `flickpond/worker` repository; PutMetricData conditioned on the
      `Flickpond` namespace; `autoscaling:CompleteLifecycleAction` on this
      worker group's ARN only.
- [ ] The launch template's `user_data` decodes to a script that fetches
      `param postgres_dsn`, `param redis_host`, `param redis_password` and
      contains no password, DSN or endpoint:
      `terraform show -json workers.plan | jq -r '.planned_values.root_module.resources[] | select(.address=="aws_launch_template.worker") | .values.user_data' | base64 -d`
- [ ] `worker_image` is tagged with a full 40-character SHA that exists in ECR.
- [ ] `terraform -chdir=../terraform plan -var-file=staging.tfvars` still says
      **No changes** (the base does not fight the added rules).

## Proving it after the apply

```bash
# The worker group admits nothing.
aws ec2 describe-security-groups --group-ids <worker_security_group_id> \
  --query 'SecurityGroups[0].IpPermissions'                     # []

# Each datastore admits exactly two groups: the core's and the workers'.
aws ec2 describe-security-group-rules \
  --filters Name=group-id,Values=<db_security_group_id> \
  --query 'SecurityGroupRules[?!IsEgress].[ReferencedGroupInfo.GroupId,CidrIpv4,FromPort]' --output table
#   same for <cache_security_group_id>; the CidrIpv4 column stays empty

# From outside AWS, both still fail: repeat datastore-review.md's nc test.
```

From a worker (`aws ssm start-session --target <instance id>`):
`sudo docker logs worker` shows RQ listening on `video_jobs` and
`video_jobs-ladder`, and a test upload goes `queued → processing → done`
(Wednesday's checkpoint).

## If something is wrong

`terraform apply -var-file=workers.tfvars -var open_datastores_to_workers=false`
removes the two ingress rules and nothing else; the workers then fail to
connect and restart, so also set the group's desired and minimum capacity to
0. If a secret may have leaked, rotate it in the base as
`datastore-review.md` describes, then **start an instance refresh**
(`aws autoscaling start-instance-refresh --auto-scaling-group-name <name>`):
a worker reads its secrets once at boot, so restarting its container keeps
the old password.

## D's read of `datastore-review.md` (Tuesday)

Agreed, with three notes for A:

1. **Rotation reaches workers only through an instance refresh**, not a
   restart (above). Worth one line in that note's last section.
2. *"`cache_endpoint` does not even resolve publicly"* — AWS endpoint names
   for private resources may well resolve from outside to their private
   `172.31.x.x` address. The test still holds if the connection times out;
   a successful DNS lookup alone is not a failure.
3. Its secrets list names `postgres_dsn` and `redis_password`; the workers
   also read `redis_host` (a plain String). Please keep that name stable.

## Changed in `infra/workers` for this review

The default instance types were sprint 4's 8 vCPU `*.2xlarge`. Sprint 5
decided `c7g.xlarge` (4 vCPU, the ≈ $11–33/month estimate); the defaults
are now the five Graviton `*.xlarge` types. If Thursday's 4K timing misses
the job timeout, set `instance_types` to the `2xlarge` sizes in
`workers.tfvars`.
