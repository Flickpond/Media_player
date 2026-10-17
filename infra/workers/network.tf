# Worker hosts accept no connections at all: they pull work from the queue.
# What they may reach is listed here: AWS APIs over HTTPS, and RDS and
# ElastiCache through the security groups the base gives each of them
# (contract 2.1). Nothing on the core host is opened to the workers.

resource "aws_security_group" "worker" {
  name        = "flickpond-${var.environment}-worker"
  description = "Autoscaling workers: no inbound; outbound to AWS APIs and the managed datastores"
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
  for_each = local.datastores

  security_group_id            = aws_security_group.worker.id
  description                  = "${each.key}, managed by AWS"
  referenced_security_group_id = each.value.security_group_id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

# The other half, on the base's RDS and ElastiCache security groups. A
# separate rule resource adds to a group without taking ownership of it, so
# the base's own `apply` neither removes these nor sees a diff. Off by
# default: opening a datastore is reviewed before it is applied (sprint 5
# rule 7, docs/datastore-review.md).
resource "aws_vpc_security_group_ingress_rule" "datastores_from_workers" {
  for_each = var.open_datastores_to_workers ? local.datastores : {}

  security_group_id            = each.value.security_group_id
  description                  = "${each.key} from autoscaling workers only"
  referenced_security_group_id = aws_security_group.worker.id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

locals {
  datastores = {
    Postgres = {
      security_group_id = var.db_security_group_id
      port              = var.db_port
    }
    Valkey = {
      security_group_id = var.cache_security_group_id
      port              = var.cache_port
    }
  }
}

check "workers_can_reach_the_datastores" {
  assert {
    condition     = var.open_datastores_to_workers
    error_message = "open_datastores_to_workers is off: workers in this plan cannot reach RDS or ElastiCache. Turn it on once docs/datastore-review.md has been read by someone other than whoever applies; do not apply like this."
  }
}
