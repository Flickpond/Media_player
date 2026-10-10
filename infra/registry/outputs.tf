# Repository variables for .github/workflows/worker-image.yml, and the
# inputs the sprint 5 worker module needs to pull.

output "repository_name" {
  description = "GitHub repository variable ECR_REPOSITORY."
  value       = aws_ecr_repository.worker.name
}

output "region" {
  description = "GitHub repository variable AWS_REGION."
  value       = var.region
}

output "ci_push_role_arn" {
  description = "GitHub repository variable AWS_ECR_PUSH_ROLE_ARN."
  value       = aws_iam_role.ci_push.arn
}

output "repository_url" {
  description = "Image to boot: <repository_url>:<full commit SHA>."
  value       = aws_ecr_repository.worker.repository_url
}

output "repository_arn" {
  description = "What the worker role's pull permissions are scoped to."
  value       = aws_ecr_repository.worker.arn
}
