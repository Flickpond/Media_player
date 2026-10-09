output "autoscaling_group_name" {
  description = "aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <this>"
  value       = aws_autoscaling_group.worker.name
}

output "worker_security_group_id" {
  description = "Sprint 5 opens Postgres and Redis (RDS and ElastiCache, #62) to this group only."
  value       = aws_security_group.worker.id
}

output "launch_template_id" {
  value = aws_launch_template.worker.id
}

output "parameter_path" {
  description = "Create redis-password and postgres-password under this path before the first apply."
  value       = local.parameter_path
}

output "image_tag" {
  description = "The commit every new worker boots."
  value       = local.image_parts.tag
}
