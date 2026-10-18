# Workers have a role of their own (sprint 6 plan). Until then they shared
# the core's instance profile, and so could read every parameter under
# /flickpond/<env>/ -- the CloudFront signing key among them -- that no worker
# ever uses. Now each side holds what it does and nothing else.
#
# The core's role (the base's app_role_name) still gets one inline policy from
# here, for the two things the core does about workers: publishing the queue
# depth they scale on, and waking the group from zero when a job is queued.

data "aws_caller_identity" "current" {}

locals {
  # Parsed from the pinned image, so the pull permission and the image to
  # pull cannot name two different repositories.
  image_parts    = regex("^(?P<account>[0-9]{12})\\.dkr\\.ecr\\.(?P<region>[a-z0-9-]+)\\.amazonaws\\.com/(?P<repository>[^:]+):(?P<tag>[0-9a-f]{40})$", var.worker_image)
  registry       = split("/", var.worker_image)[0]
  repository_arn = "arn:aws:ecr:${local.image_parts.region}:${local.image_parts.account}:repository/${local.image_parts.repository}"
  parameter_path = "/flickpond/${var.environment}"
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "worker" {
  name               = "flickpond-${var.environment}-worker"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_instance_profile" "worker" {
  name = "flickpond-${var.environment}-worker"
  role = aws_iam_role.worker.name
}

# No Session Manager policy on purpose: AmazonSSMManagedInstanceCore grants
# ssm:GetParameter on every parameter, which would undo the narrowing below.
# A worker is disposable; to look inside one, read its logs or replace it.
data "aws_iam_policy_document" "worker" {
  # What the encoders do to objects: read sources, write outputs and ladders,
  # remove a ladder nobody will record (which lists its prefix first).
  statement {
    sid       = "ListTheVideosBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"]
    resources = ["arn:aws:s3:::${var.bucket_name}"]
  }

  statement {
    sid = "ReadWriteVideos"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["arn:aws:s3:::${var.bucket_name}/*"]
  }

  statement {
    sid       = "LogInToTheRegistry"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # no resource-level permissions
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

  # Exactly the three settings user data reads at boot (written by the base,
  # infra/terraform/datastores.tf), not the path they live under. Decryption
  # uses the AWS-managed aws/ssm key, which needs no kms:Decrypt grant.
  statement {
    sid     = "ReadTheConnectionSettings"
    actions = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [
      for name in ["postgres_dsn", "redis_host", "redis_password"] :
      "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.parameter_path}/${name}"
    ]
  }

  # The drain service tells the group its worker has given its job back, so
  # a scale-in ends in seconds rather than at the hook's timeout.
  statement {
    sid       = "FinishDrainingThisGroup"
    actions   = ["autoscaling:CompleteLifecycleAction"]
    resources = [aws_autoscaling_group.worker.arn]
  }

  # A chunked MP4's coordinator starts workers for the helpers it queues
  # (app/services/worker_wake.py). Up only, and never past max_size.
  statement {
    sid       = "StartWorkersForHelpers"
    actions   = ["autoscaling:SetDesiredCapacity"]
    resources = [aws_autoscaling_group.worker.arn]
  }

  statement {
    sid       = "SeeHowManyAreRunning"
    actions   = ["autoscaling:DescribeAutoScalingGroups"]
    resources = ["*"] # Describe* has no resource-level permissions
  }
}

resource "aws_iam_role_policy" "worker" {
  name   = "worker"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

data "aws_iam_policy_document" "core" {
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

  # app/services/worker_wake.py: one worker, from zero, as a job is queued.
  statement {
    sid       = "WakeThisGroup"
    actions   = ["autoscaling:SetDesiredCapacity"]
    resources = [aws_autoscaling_group.worker.arn]
  }

  statement {
    sid       = "SeeWhetherItIsAwake"
    actions   = ["autoscaling:DescribeAutoScalingGroups"]
    resources = ["*"] # Describe* has no resource-level permissions
  }
}

# Same name and address as before, so the apply updates it in place.
resource "aws_iam_role_policy" "workers" {
  name   = "autoscaling-workers"
  role   = var.app_role_name
  policy = data.aws_iam_policy_document.core.json
}
