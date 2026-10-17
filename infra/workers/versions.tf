terraform {
  # 1.10 like the base, for the S3 backend's native lock file; `terraform
  # test` with mock providers (tests/) needs 1.7.
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # In the base's state bucket, one key per environment, locked while anyone
  # applies: this root adds rules to the base's security groups and a policy
  # to its role, and two laptops with two private copies of that truth would
  # undo each other. Settings from a file, like the base:
  #   terraform init -backend-config=staging.backend.hcl
  # The offline tests need none of it: terraform init -backend=false.
  backend "s3" {}
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
