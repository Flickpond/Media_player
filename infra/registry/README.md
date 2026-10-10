# Image registry (sprint 4, Track D)

Sprint 5's autoscaling workers boot from an image in ECR instead of building
one. This root creates the repository and the role CI pushes it with. It is
**separate from the base** in [`../terraform`](../terraform) (Track A's), so it
can be applied on its own and the base stays untouched.

| File | What |
|---|---|
| `ecr.tf` | `flickpond/worker`: immutable tags, scan on push, the newest 30 images kept |
| `ci-role.tf` | GitHub OIDC provider and `flickpond-ci-ecr-push`, which only a workflow run on `main` of this repository can assume, and which can only push to that one repository |
| `outputs.tf` | The three GitHub repository variables, and what the worker module pulls with |

No AWS access key exists anywhere: GitHub signs a short-lived token for each
run, and AWS trusts it for one repository and one branch.

## Apply

Needs credentials that can create IAM roles and an OIDC provider.

```bash
cd infra/registry
aws iam list-open-id-connect-providers   # one for token.actions.githubusercontent.com already?
terraform init
terraform plan -out=registry.plan        # add -var create_github_oidc_provider=false if it exists
terraform apply registry.plan
terraform output
```

Then, in GitHub, under **Settings → Secrets and variables → Actions →
Variables**, add three repository variables (no secrets are needed):

| Variable | From |
|---|---|
| `AWS_ECR_PUSH_ROLE_ARN` | `terraform output -raw ci_push_role_arn` |
| `AWS_REGION` | `terraform output -raw region` |
| `ECR_REPOSITORY` | `terraform output -raw repository_name` |

The next push to `main` that passes CI pushes
`<repository_url>:<full commit SHA>`. To push the current `main` straight
away, run the **Worker image** workflow by hand from `main`.

## What the worker module needs from here

Sprint 5's workers pull with their instance role. Scope the policy to
`repository_arn`, plus `ecr:GetAuthorizationToken` on `*` (that action has no
resource-level permissions):

```text
ecr:GetAuthorizationToken      *
ecr:BatchGetImage              <repository_arn>
ecr:GetDownloadUrlForLayer     <repository_arn>
ecr:BatchCheckLayerAvailability <repository_arn>
```

## Cost, roughly

ECR storage is about US$0.10 per GB-month. The image is a few hundred MB,
and layers shared between images are stored once, so 30 images cost cents a
month. Pulls inside the same region are free. Check the AWS pricing page
before relying on these.
