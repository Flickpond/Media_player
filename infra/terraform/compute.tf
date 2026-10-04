# One host running the same Compose stack as the current server, minus
# MinIO. D's autoscaling workers (sprint 5) join this VPC and security group
# through the outputs; this file is the part they must not edit.

# The default VPC: public subnets in every zone and no NAT gateway to pay
# for. A dedicated VPC arrives with sprint 5, when workers on other hosts
# need Postgres and Redis on a private network.
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }

  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

# Looked up rather than pinned, so a fresh `apply` gets current patches.
data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_security_group" "app" {
  name        = "flickpond-${var.environment}-app"
  description = "nginx is the only thing published, as on the current server"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.app.id
  description       = "Redirects to HTTPS and serves the ACME challenge"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.app.id
  description       = "The site"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  for_each = toset(var.admin_cidrs)

  security_group_id = aws_security_group.app.id
  description       = "SSH for an operator; Session Manager needs no rule"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
}

# Outbound is web traffic only. DNS (the VPC resolver) and time
# (169.254.169.123) are not filtered by security groups, so nothing else is
# needed. The destination stays open because Docker Hub, GitHub and Let's
# Encrypt publish no stable address ranges; S3 is reached the same way.
# trivy:ignore:AVD-AWS-0104
resource "aws_vpc_security_group_egress_rule" "web" {
  for_each = toset(["80", "443"])

  security_group_id = aws_security_group.app.id
  description       = "Image pulls, S3, Let's Encrypt, package updates"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.value)
  to_port           = tonumber(each.value)
}

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.al2023_arm64.value
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.app.name
  key_name               = var.ssh_key_name

  # IMDSv2 only. With v1, anything that can make the app fetch a URL can
  # read the role's credentials -- and this role can read every video.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2 # containers are one hop further away
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_gb
    encrypted   = true
  }

  user_data                   = file("${path.module}/user-data.sh")
  user_data_replace_on_change = true

  # A newer AMI must not replace a running staging server on the next plan.
  # Re-image deliberately with `-replace=aws_instance.app`.
  lifecycle {
    ignore_changes = [ami]
  }

  tags = {
    Name = "flickpond-${var.environment}"
  }
}

# A fixed address for DNS that survives stop/start, which the instance's own
# public IP does not.
resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.app.id

  tags = {
    Name = "flickpond-${var.environment}"
  }
}
