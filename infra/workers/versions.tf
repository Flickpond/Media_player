terraform {
  # 1.6 like the base; `terraform test` with mock providers (tests/) needs 1.7.
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # Local state for `plan` in sprint 4. Before the first `apply` (sprint 5)
  # this moves to the S3 backend with locking, together with the base: two
  # roots that both touch the base's role and security group must not be
  # applied from two laptops with two private copies of the truth.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "flickpond"
      Environment = var.environment
      ManagedBy   = "terraform"
      Component   = "workers"
    }
  }
}
