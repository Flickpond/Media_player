terraform {
  # 1.10 like the base, for the S3 backend's native lock file.
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # In the base's state bucket, locked while anyone applies. One key for the
  # account: the repository and CI role serve every environment.
  #   terraform init -backend-config=registry.backend.hcl
  backend "s3" {}
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
