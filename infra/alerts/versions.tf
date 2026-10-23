terraform {
  # 1.10 like the other roots, for the S3 backend's native lock file;
  # `terraform test` with mock providers (tests/) needs 1.7.
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # In the base's state bucket, under its own key, like infra/workers:
  #   terraform init -backend-config=staging.backend.hcl
  # The offline tests need none of it: terraform init -backend=false.
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.tags
  }
}

# Route 53 publishes health-check metrics to us-east-1 only, and an alarm
# can only notify a topic in its own region: the site-down alarm and the
# topic it tells live there, whatever region the site runs in.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = local.tags
  }
}

locals {
  tags = {
    Project     = "flickpond"
    Environment = var.environment
    ManagedBy   = "terraform"
    Component   = "alerts"
  }
}
