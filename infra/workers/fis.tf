# The spot interruption test (sprint 5, Thursday): AWS Fault Injection
# Service sends one worker a real spot interruption notice, two minutes out,
# exactly as a reclaim would. The only way to exercise the drain's spot path
# (user-data.sh.tftpl) without waiting for AWS to need the capacity back.
#
# Off by default, so a normal plan creates none of this. Turn it on for the
# test, run it, turn it off again (docs/d-aws-handoff.md, step 6). Each run
# costs a few cents of FIS action time.

resource "aws_iam_role" "interruption_test" {
  count = var.interruption_test ? 1 : 0

  name = "flickpond-${var.environment}-worker-interruption-test"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "fis.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Interrupts this environment's workers and nothing else: the instances carry
# the group's name tag, which EC2 Auto Scaling sets and nobody can edit.
resource "aws_iam_role_policy" "interruption_test" {
  count = var.interruption_test ? 1 : 0

  name = "interrupt-this-environments-workers"
  role = aws_iam_role.interruption_test[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InterruptOnlyTheseWorkers"
        Effect   = "Allow"
        Action   = "ec2:SendSpotInstanceInterruptions"
        Resource = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals = { "ec2:ResourceTag/aws:autoscaling:groupName" = local.group_name }
        }
      },
      {
        Sid      = "FindThem"
        Effect   = "Allow"
        Action   = "ec2:DescribeInstances"
        Resource = "*" # no resource-level permissions
      },
    ]
  })
}

resource "aws_fis_experiment_template" "interruption_test" {
  count = var.interruption_test ? 1 : 0

  description = "Spot interruption notice to one ${var.environment} worker (contract 2.3)"
  role_arn    = aws_iam_role.interruption_test[0].arn

  stop_condition {
    source = "none"
  }

  action {
    name      = "interrupt-one-worker"
    action_id = "aws:ec2:send-spot-instance-interruptions"

    # Two minutes, the same warning a real reclaim gives.
    parameter {
      key   = "durationBeforeInterruption"
      value = "PT2M"
    }

    target {
      key   = "SpotInstances"
      value = "one-running-worker"
    }
  }

  target {
    name           = "one-running-worker"
    resource_type  = "aws:ec2:spot-instance"
    selection_mode = "COUNT(1)"

    resource_tag {
      key   = "aws:autoscaling:groupName"
      value = local.group_name
    }

    filter {
      path   = "State.Name"
      values = ["running"]
    }
  }

  tags = {
    Name = "flickpond-${var.environment}-worker-interruption-test"
  }
}
