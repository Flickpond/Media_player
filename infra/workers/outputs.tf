output "autoscaling_group_name" {
  description = "aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names <this>"
  value       = aws_autoscaling_group.worker.name
}

output "worker_security_group_id" {
  description = "The group RDS and ElastiCache admit, once open_datastores_to_workers is on."
  value       = aws_security_group.worker.id
}

output "launch_template_id" {
  value = aws_launch_template.worker.id
}

output "parameter_path" {
  description = "Workers read postgres_dsn, redis_host and redis_password here at boot; the base writes them."
  value       = local.parameter_path
}

output "image_tag" {
  description = "The commit every new worker boots."
  value       = local.image_parts.tag
}

output "interruption_test_template_id" {
  description = "aws fis start-experiment --experiment-template-id <this>. Null unless interruption_test is on."
  value       = one(aws_fis_experiment_template.interruption_test[*].id)
}
