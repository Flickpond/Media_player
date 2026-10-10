terraform {
  # 1.10 for the S3 backend's native lock file: no DynamoDB table to pay for.
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    # The CloudFront signing key pair (cdn.tf).
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }

  # State lives in S3, one key per environment, locked while anyone applies.
  # It holds the generated database and queue passwords, so the bucket is
  # private, encrypted and versioned -- see infra/bootstrap, which creates
  # it. The settings come from a file, so the same code serves staging and
  # production:
  #   terraform init -backend-config=staging.backend.hcl
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "flickpond"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
