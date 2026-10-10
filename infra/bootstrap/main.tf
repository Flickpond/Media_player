# The bucket every other Terraform root keeps its state in. Applied once per
# AWS account, by hand, with local state: it is the one thing that cannot
# keep its own state in the bucket it creates. Losing this directory's state
# loses nothing -- the bucket is found again by name.
#
#   terraform init && terraform apply
#   terraform output -raw state_bucket   # goes into each *.backend.hcl

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

variable "region" {
  type    = string
  default = "ap-southeast-1"
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "flickpond"
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

# Account id in the name: bucket names are global, and this one must never
# collide with -- or be squatted by -- someone else's.
#
# Accepted finding: AVD-AWS-0089 access logging -- a second bucket to log the
# first; changes to the state are already recorded by its versioning.
# trivy:ignore:AVD-AWS-0089
resource "aws_s3_bucket" "state" {
  bucket = "flickpond-tfstate-${data.aws_caller_identity.current.account_id}"

  # The state of every environment lives here; destroying it by accident
  # would orphan every resource Terraform manages.
  lifecycle {
    prevent_destroy = true
  }
}

# Every apply writes a new version, so a corrupted or mistaken state can be
# rolled back to the one before it.
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

# SSE-S3, not a customer-managed key (AVD-AWS-0132): encrypted at rest either
# way, and a KMS key adds a monthly charge for no reader we need to keep out.
# trivy:ignore:AVD-AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}
