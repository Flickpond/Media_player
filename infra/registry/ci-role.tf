# How CI pushes without an AWS key anywhere. GitHub signs a short-lived token
# for each workflow run; AWS trusts it for exactly one repository and one
# branch, and hands back credentials that last as long as the job.

locals {
  github_oidc_url = "https://token.actions.githubusercontent.com"
  github_oidc_provider_arn = (
    var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : data.aws_iam_openid_connect_provider.github[0].arn
  )
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url            = local.github_oidc_url
  client_id_list = ["sts.amazonaws.com"]
  # AWS no longer checks these for GitHub (it trusts GitHub's certificate
  # authority directly), but older provider versions still require the list.
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 0 : 1
  url   = local.github_oidc_url
}

data "aws_iam_policy_document" "ci_assume" {
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

    # Exact match, not StringLike with a wildcard: a pull request's token says
    # `pull_request`, and another branch's says its own ref, so neither can
    # push an image a worker might boot.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:ref:refs/heads/${var.push_branch}"]
    }
  }
}

resource "aws_iam_role" "ci_push" {
  name                 = "flickpond-ci-ecr-push"
  assume_role_policy   = data.aws_iam_policy_document.ci_assume.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "ci_push" {
  # "*" because ecr:GetAuthorizationToken has no resource-level permissions;
  # everything that writes is scoped to the one repository below.
  statement {
    sid       = "LogInToTheRegistry"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "PushTheWorkerImage"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      # Lets a re-run see the commit is already pushed: tags are immutable,
      # so pushing it again would fail rather than do nothing.
      "ecr:DescribeImages",
    ]
    resources = [aws_ecr_repository.worker.arn]
  }
}

resource "aws_iam_role_policy" "ci_push" {
  name   = "push-worker-image"
  role   = aws_iam_role.ci_push.id
  policy = data.aws_iam_policy_document.ci_push.json
}
