# Spot workers, 0..max_workers, booting the image CI pushed for one commit.

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_launch_template" "worker" {
  name_prefix            = "flickpond-${var.environment}-worker-"
  description            = "Autoscaling worker, ${local.image_parts.tag}"
  image_id               = data.aws_ssm_parameter.al2023_arm64.value
  update_default_version = true

  iam_instance_profile {
    name = var.instance_profile_name
  }

  # Public address, no inbound rule: the default VPC has no NAT gateway, and
  # this is how the instance reaches ECR and S3 without paying for one.
  network_interfaces {
    associate_public_ip_address = true
    delete_on_termination       = true
    security_groups             = [aws_security_group.worker.id]
  }

  # IMDSv2 only, as on the base: the role these credentials belong to can
  # read every video.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_type           = "gp3"
      volume_size           = var.root_volume_gb
      encrypted             = true
      delete_on_termination = true
    }
  }

  # No secrets in here: anyone allowed to describe launch templates can read
  # user data. The passwords are fetched from Parameter Store at boot.
  user_data = base64encode(templatefile("${path.module}/user-data.sh.tftpl", {
    region          = var.region
    registry        = local.registry
    image           = var.worker_image
    parameter_path  = local.parameter_path
    core_private_ip = var.core_private_ip
    bucket_name     = var.bucket_name
    worker_settings = var.worker_settings
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "flickpond-${var.environment}-worker"
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags = {
      Name = "flickpond-${var.environment}-worker"
    }
  }
}

resource "aws_autoscaling_group" "worker" {
  name                = "flickpond-${var.environment}-workers"
  vpc_zone_identifier = var.subnet_ids

  # The schedules below own min_size, and the scaling policies own desired
  # capacity; Terraform sets their starting values and then leaves them be.
  min_size = 0
  max_size = var.max_workers

  # The group may legitimately have no capacity yet (spot, or a zero
  # minimum overnight): an apply must not hang waiting for instances.
  wait_for_capacity_timeout = "0"

  health_check_type         = "EC2"
  health_check_grace_period = 120
  default_instance_warmup   = 180

  # Start a replacement as soon as AWS warns that a spot instance is at
  # elevated risk, rather than only after it is reclaimed.
  capacity_rebalance = true

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0
      spot_allocation_strategy                 = "price-capacity-optimized"
    }

    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.worker.id
        version            = aws_launch_template.worker.latest_version
      }

      dynamic "override" {
        for_each = var.instance_types
        content {
          instance_type = override.value
        }
      }
    }
  }

  # One-minute group metrics, free, so the console shows capacity next to
  # the queue it is reacting to.
  enabled_metrics = [
    "GroupDesiredCapacity",
    "GroupInServiceInstances",
    "GroupPendingInstances",
    "GroupTotalInstances",
  ]

  tag {
    key                 = "Name"
    value               = "flickpond-${var.environment}-worker"
    propagate_at_launch = true
  }

  lifecycle {
    ignore_changes = [min_size, desired_capacity]
  }
}

# The scheduled minimum: one worker kept warm by day, none overnight.
#
# max_size and desired_capacity are -1 ("leave unchanged") on purpose. The
# provider defaults both to 0, so omitting them would empty the group every
# night -- including workers in the middle of an encode.
resource "aws_autoscaling_schedule" "daytime" {
  scheduled_action_name  = "daytime-minimum"
  autoscaling_group_name = aws_autoscaling_group.worker.name
  recurrence             = var.daytime_starts
  time_zone              = var.schedule_time_zone
  min_size               = var.daytime_min_workers
  max_size               = -1
  desired_capacity       = -1

  lifecycle {
    precondition {
      condition     = var.daytime_min_workers <= var.max_workers
      error_message = "daytime_min_workers cannot exceed max_workers."
    }
  }
}

# Lowering the minimum terminates nothing by itself. Workers still running
# jobs keep running; the idle scale-in in scaling.tf removes them once the
# queue is empty.
resource "aws_autoscaling_schedule" "overnight" {
  scheduled_action_name  = "overnight-minimum"
  autoscaling_group_name = aws_autoscaling_group.worker.name
  recurrence             = var.overnight_starts
  time_zone              = var.schedule_time_zone
  min_size               = 0
  max_size               = -1
  desired_capacity       = -1
}
