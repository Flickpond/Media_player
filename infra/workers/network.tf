# Worker hosts accept no connections at all: they pull work from Redis. What
# they may reach is listed here, and Postgres and Redis only on the core host.

resource "aws_security_group" "worker" {
  name        = "flickpond-${var.environment}-worker"
  description = "Autoscaling workers: no inbound; outbound to AWS APIs and the core datastores"
  vpc_id      = var.vpc_id
}

# ECR, S3, SSM Parameter Store and the Amazon Linux package repositories are
# all HTTPS on address ranges too wide to list.
# trivy:ignore:AVD-AWS-0104
resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.worker.id
  description       = "ECR, S3, SSM and package repositories"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "datastores" {
  for_each = local.datastore_ports

  security_group_id            = aws_security_group.worker.id
  description                  = "${each.key} on the core host"
  referenced_security_group_id = var.security_group_id
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
}

# The other half, on the base's security group. A separate rule resource adds
# to that group without taking ownership of it, so the base's own `apply`
# neither removes these nor sees a diff. Off until sprint 5.
resource "aws_vpc_security_group_ingress_rule" "datastores_from_workers" {
  for_each = var.open_datastores_to_workers ? local.datastore_ports : {}

  security_group_id            = var.security_group_id
  description                  = "${each.key} from autoscaling workers only"
  referenced_security_group_id = aws_security_group.worker.id
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
}

locals {
  datastore_ports = {
    Postgres = 5432
    Redis    = 6379
  }
}

check "workers_can_reach_the_datastores" {
  assert {
    condition     = var.open_datastores_to_workers
    error_message = "open_datastores_to_workers is off: workers in this plan cannot reach Postgres or Redis. Expected in sprint 4 (plan only); do not apply like this."
  }
}
