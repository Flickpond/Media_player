# Added to the base's role as one more inline policy, as agreed: the base
# exposes app_role_name for exactly this, so its files stay untouched.
#
# The core host gains these too, because workers share its instance profile.
# It needs PutMetricData anyway (the queue-depth publisher runs there); ECR
# pull and reading this environment's parameters are harmless additions on a
# host that already holds both secrets in its .env.

data "aws_caller_identity" "current" {}

locals {
  # Parsed from the pinned image, so the pull permission and the image to
  # pull cannot name two different repositories.
  image_parts    = regex("^(?P<account>[0-9]{12})\\.dkr\\.ecr\\.(?P<region>[a-z0-9-]+)\\.amazonaws\\.com/(?P<repository>[^:]+):(?P<tag>[0-9a-f]{40})$", var.worker_image)
  registry       = split("/", var.worker_image)[0]
  repository_arn = "arn:aws:ecr:${local.image_parts.region}:${local.image_parts.account}:repository/${local.image_parts.repository}"
  parameter_path = "/flickpond/${var.environment}"
}

data "aws_iam_policy_document" "workers" {
  statement {
    sid       = "PublishQueueDepth"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"] # PutMetricData has no resource-level permissions
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [var.metrics_namespace]
    }
  }

  statement {
    sid       = "LogInToTheRegistry"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # no resource-level permissions either
  }

  statement {
    sid = "PullTheWorkerImage"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [local.repository_arn]
  }

  # The Redis and Postgres passwords, created by hand as SecureStrings (see
  # README.md) so they never enter Terraform state or the launch template.
  # Decryption uses the AWS-managed aws/ssm key, which needs no kms:Decrypt
  # grant of its own.
  statement {
    sid     = "ReadThisEnvironmentsSecrets"
    actions = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [
      "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.parameter_path}/*",
    ]
  }
}

resource "aws_iam_role_policy" "workers" {
  name   = "autoscaling-workers"
  role   = var.app_role_name
  policy = data.aws_iam_policy_document.workers.json
}
