# The instance gets its S3 access from a role, not from keys in `.env`. No
# long-lived secret exists to leak, and the credentials rotate on their own.

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "app" {
  name               = "flickpond-${var.environment}-app"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# Exactly what the API, worker and reaper do to objects today, on this one
# bucket and nothing else. Every URL the API signs carries these permissions
# and no more, because a presigned URL can only do what its signer can.
data "aws_iam_policy_document" "videos_bucket" {
  statement {
    sid       = "ListForReaperAndHlsCleanup"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"]
    resources = [aws_s3_bucket.videos.arn]
  }

  statement {
    sid = "ReadWriteObjects"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${aws_s3_bucket.videos.arn}/*"]
  }
}

resource "aws_iam_role_policy" "videos_bucket" {
  name   = "videos-bucket"
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.videos_bucket.json
}

# Session Manager: a shell on the instance without port 22 open, logged in
# CloudTrail. The fallback when `admin_cidrs` is empty or an IP has changed.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "app" {
  name = "flickpond-${var.environment}-app"
  role = aws_iam_role.app.name
}
