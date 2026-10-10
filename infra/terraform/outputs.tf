# The interface D's autoscaling module (sprint 5) builds on. Renaming or
# removing one of these breaks that module -- treat this file like
# docs/contract.md and tell D first.

output "vpc_id" {
  value = data.aws_vpc.default.id
}

output "subnet_ids" {
  description = "One public subnet per zone, for the worker group to spread across."
  value       = data.aws_subnets.default.ids
}

output "security_group_id" {
  value = aws_security_group.app.id
}

output "instance_profile_name" {
  description = "Workers need the same bucket access as this host."
  value       = aws_iam_instance_profile.app.name
}

output "app_role_name" {
  description = "For attaching further policies (e.g. CloudWatch metrics) without editing the base."
  value       = aws_iam_role.app.name
}

output "bucket_name" {
  description = "MINIO_BUCKET in the instance's .env."
  value       = aws_s3_bucket.videos.bucket
}

output "region" {
  description = "MINIO_REGION in the instance's .env."
  value       = var.region
}

output "public_ip" {
  description = "Point the staging DNS record here."
  value       = aws_eip.app.public_ip
}

output "instance_id" {
  description = "aws ssm start-session --target <this>"
  value       = aws_instance.app.id
}

# --- sprint 5: the datastores (contract 2.1) ---------------------------------

output "core_private_ip" {
  description = "The core host on the private network."
  value       = aws_instance.app.private_ip
}

output "db_endpoint" {
  value = aws_db_instance.main.address
}

output "db_port" {
  value = aws_db_instance.main.port
}

output "db_security_group_id" {
  description = "The worker module adds its own ingress rule here."
  value       = aws_security_group.db.id
}

output "cache_endpoint" {
  description = "Valkey primary endpoint; TLS only."
  value       = aws_elasticache_replication_group.main.primary_endpoint_address
}

output "cache_port" {
  value = aws_elasticache_replication_group.main.port
}

output "cache_security_group_id" {
  description = "The worker module adds its own ingress rule here."
  value       = aws_security_group.cache.id
}

# Names of the secrets, never their values.
output "db_secret_parameter" {
  value = aws_ssm_parameter.db_dsn.name
}

output "cache_secret_parameter" {
  value = aws_ssm_parameter.cache_token.name
}
