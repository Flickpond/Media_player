# Deploying the core from CI, by hand only (.github/workflows/deploy.yml).
#
# No SSH key in GitHub. A run of the deploy workflow gets a short-lived token
# for the `production` environment -- which requires a reviewer's approval
# before every run -- and AWS trusts that token for one thing: running ONE
# fixed command document on ONE instance. The document holds the deploy steps
# themselves, so even a workflow edited on a branch cannot make this role run
# anything else on the core.
#
# Off unless `core_instance_id` is set (infra/terraform output `core_instance_id`).

resource "aws_ssm_document" "deploy_core" {
  count = var.core_instance_id == null ? 0 : 1

  name            = "flickpond-deploy-core"
  document_type   = "Command"
  document_format = "YAML"
  content         = <<-YAML
    schemaVersion: "2.2"
    description: "Deploy origin/main to the Flickpond core. No parameters by design."
    mainSteps:
      - action: aws:runShellScript
        name: deploy
        inputs:
          timeoutSeconds: "1800"
          runCommand:
            - set -euo pipefail
            - cd /srv/Media_player
            # SSM runs as root; the checkout is ec2-user's. Trusted for these
            # two commands only, not in root's global git config.
            - git -c safe.directory=/srv/Media_player fetch origin
            - git -c safe.directory=/srv/Media_player reset --hard origin/main
            - bash deploy/aws/fetch-secrets.sh staging >/dev/null
            # The date re-runs each image's package upgrade once a day, as in
            # CI; the host's build cache would otherwise keep the first day's.
            - APT_REFRESH="$(date -u +%F)" docker compose -f docker-compose.yml -f deploy/aws/compose.aws.yml -f deploy/aws/compose.core.yml up -d --build
            - sleep 20
            - git -c safe.directory=/srv/Media_player log --oneline -1
            - curl -fsS https://flickpond.com/api/health
  YAML
}

data "aws_iam_policy_document" "deploy_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # A job that names `environment: production` gets this subject, and only
    # after the environment's required reviewer has approved the run.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:environment:production"]
    }
  }
}

resource "aws_iam_role" "ci_deploy" {
  count = var.core_instance_id == null ? 0 : 1

  name                 = "flickpond-ci-deploy-core"
  assume_role_policy   = data.aws_iam_policy_document.deploy_assume.json
  max_session_duration = 3600
}

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "ci_deploy" {
  count = var.core_instance_id == null ? 0 : 1

  statement {
    sid     = "RunTheDeployDocumentOnTheCore"
    actions = ["ssm:SendCommand"]
    resources = [
      aws_ssm_document.deploy_core[0].arn,
      "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/${var.core_instance_id}",
    ]
  }

  statement {
    sid       = "ReadWhatItSaid"
    actions   = ["ssm:GetCommandInvocation"]
    resources = ["*"] # no resource-level permissions
  }
}

resource "aws_iam_role_policy" "ci_deploy" {
  count = var.core_instance_id == null ? 0 : 1

  name   = "deploy-core"
  role   = aws_iam_role.ci_deploy[0].id
  policy = data.aws_iam_policy_document.ci_deploy[0].json
}
