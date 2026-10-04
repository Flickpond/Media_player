terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State is local for now: one operator, one staging environment, nothing
  # secret in it. Moving it to an S3 backend with locking is the first thing
  # sprint 5 does, before a second person -- or D's autoscaling module -- ever
  # runs `apply` against the same resources.
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
