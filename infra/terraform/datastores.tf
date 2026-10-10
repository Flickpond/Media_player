# Postgres and Redis as AWS-managed services, so the core host and the
# autoscaling workers (infra/workers) share one database and one queue over
# the private network. Sprint 5 decision: managed rather than on the core --
# RDS brings backups nobody has to remember; ElastiCache cannot be given a
# public address at all, and anything that can write to the RQ queue can make
# a worker run code of its choosing.
#
# Both admit only the core here. The worker module adds its own rule to each
# security group, the way it already does for the core's (contract 2.1).
#
# Read docs/datastore-review.md before the first apply.

locals {
  name = "flickpond-${var.environment}"
}

# --- PostgreSQL -------------------------------------------------------------

# Letters and digits only: the password is interpolated into a DSN, where
# `@`, `/` and `:` would each need escaping and one missed escape silently
# points the app at the wrong host.
resource "random_password" "db" {
  length  = 40
  special = false
}

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "PostgreSQL: the core and the worker group only"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_core" {
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from the core host"
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

# Plain-text connections are refused outright, not merely discouraged.
resource "aws_db_parameter_group" "postgres" {
  name   = "${local.name}-postgres16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
}

# Accepted findings, each on purpose:
#   AVD-AWS-0176 IAM database authentication -- tokens expire every 15
#     minutes and SQLAlchemy's pool would need a refresh hook; a generated
#     password in SSM over enforced TLS is the proportionate choice here.
#   AVD-AWS-0133 Performance Insights -- a paid feature for a database this
#     size; turn it on if the login latency investigation points here.
# trivy:ignore:AVD-AWS-0176
# trivy:ignore:AVD-AWS-0133
resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = 20
  max_allocated_storage = 100
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "flickpond"
  username = "flickpond"
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.postgres.name
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period    = 7
  copy_tags_to_snapshot      = true
  auto_minor_version_upgrade = true

  # Production keeps its data through a mistaken destroy; staging can be
  # torn down and rebuilt freely.
  deletion_protection       = var.protect_data
  skip_final_snapshot       = !var.protect_data
  final_snapshot_identifier = var.protect_data ? "${local.name}-final" : null
}

# --- Valkey (the RQ queue) ---------------------------------------------------

resource "random_password" "cache" {
  length  = 48
  special = false
}

resource "aws_elasticache_subnet_group" "main" {
  name       = local.name
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_security_group" "cache" {
  name        = "${local.name}-cache"
  description = "Valkey: the core and the worker group only"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "cache_from_core" {
  security_group_id            = aws_security_group.cache.id
  description                  = "Valkey from the core host"
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

# A replication group rather than a bare cluster: it is the resource that
# supports in-transit encryption and an auth token. One node; the queue is
# rebuilt from Postgres by the reaper if it is ever lost, so no replica.
resource "aws_elasticache_replication_group" "main" {
  replication_group_id = local.name
  description          = "Flickpond RQ job queue"
  engine               = "valkey"
  engine_version       = "8.0"
  node_type            = var.cache_node_type
  num_cache_clusters   = 1
  port                 = 6379
  parameter_group_name = "default.valkey8"

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.cache.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.cache.result

  automatic_failover_enabled = false
  snapshot_retention_limit   = 0
  apply_immediately          = true
}

# --- Secrets ------------------------------------------------------------------

# Where every host reads its connection details from. Never Terraform
# outputs: outputs are printed, and land in CI logs and terminal scrollback.
# (They are in the state, which is why the state bucket is private and
# encrypted -- see infra/bootstrap.)
resource "aws_ssm_parameter" "db_dsn" {
  name        = "/flickpond/${var.environment}/postgres_dsn"
  description = "POSTGRES_DSN for the API, reaper and workers (TLS required)"
  type        = "SecureString"
  value       = "postgresql://flickpond:${random_password.db.result}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/flickpond?ssl=require"
}

# Not a secret, but where every host looks for its connection settings, so a
# host needs nothing from Terraform's outputs to start.
resource "aws_ssm_parameter" "cache_host" {
  name        = "/flickpond/${var.environment}/redis_host"
  description = "REDIS_HOST: the ElastiCache primary endpoint (TLS only)"
  type        = "String"
  value       = aws_elasticache_replication_group.main.primary_endpoint_address
}

resource "aws_ssm_parameter" "cache_token" {
  name        = "/flickpond/${var.environment}/redis_password"
  description = "REDIS_PASSWORD (the ElastiCache auth token)"
  type        = "SecureString"
  value       = random_password.cache.result
}
