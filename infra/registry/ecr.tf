# Where sprint 5's autoscaling workers get their image. A worker booting on a
# fresh spot instance pulls a finished image instead of building one, which
# is minutes faster and needs no repository access on the instance.

# Accepted finding:
#   AVD-AWS-0033 customer-managed KMS key -- AES256 already encrypts at rest,
#     and the image holds the same code as the public repository, no secrets.
#     A KMS key adds a monthly cost and a key policy for no added secrecy.
# trivy:ignore:AVD-AWS-0033
resource "aws_ecr_repository" "worker" {
  name = "flickpond/worker"

  # A tag names one image forever, so a launch template that names a commit
  # always boots that commit. Rolling back means naming an older commit,
  # never re-pushing a tag.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "worker" {
  repository = aws_ecr_repository.worker.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Layers an interrupted push left without a tag"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        # Must be the last rule: ECR requires "any" to have the lowest priority.
        rulePriority = 2
        description  = "Keep the newest images; storage is billed per GB"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.images_to_keep
        }
        action = { type = "expire" }
      },
    ]
  })
}
