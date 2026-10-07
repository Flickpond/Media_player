terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # Local state, like the base, and for the same reason: one operator for
  # now. It moves to the S3 backend with locking alongside the base, before
  # the worker group is ever applied (sprint 5).
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "flickpond"
      Environment = "shared"
      ManagedBy   = "terraform"
    }
  }
}
