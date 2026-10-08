# Offline: the AWS provider is mocked, so these run with no account and no
# credentials (Terraform 1.7+):
#
#   terraform init -backend=false && terraform test
#
# They pin the decisions that would fail silently: a schedule that empties
# the group, a group that is not all spot, secrets in user data, datastores
# opened by default.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
    }
  }

  mock_data "aws_ssm_parameter" {
    defaults = {
      value = "ami-0123456789abcdef0"
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

variables {
  vpc_id                = "vpc-0123456789abcdef0"
  subnet_ids            = ["subnet-0aaaaaaaaaaaaaaaa", "subnet-0bbbbbbbbbbbbbbbb"]
  security_group_id     = "sg-0core000000000000"
  instance_profile_name = "flickpond-staging-app"
  app_role_name         = "flickpond-staging-app"
  bucket_name           = "flickpond-staging-videos-abcd1234"
  region                = "ap-southeast-1"
  core_private_ip       = "172.31.5.20"
  worker_image          = "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/flickpond/worker:0123456789abcdef0123456789abcdef01234567"
}

run "the_group_is_all_spot_and_starts_empty" {
  command = plan

  # Datastores closed by default: the check warns, as it should.
  expect_failures = [check.workers_can_reach_the_datastores]

  assert {
    condition     = aws_autoscaling_group.worker.min_size == 0 && aws_autoscaling_group.worker.max_size == 4
    error_message = "The group should start at 0 and stop at max_workers."
  }

  assert {
    condition = (
      aws_autoscaling_group.worker.mixed_instances_policy[0].instances_distribution[0].on_demand_base_capacity == 0
      && aws_autoscaling_group.worker.mixed_instances_policy[0].instances_distribution[0].on_demand_percentage_above_base_capacity == 0
    )
    error_message = "Every worker should be spot."
  }

  assert {
    condition     = aws_autoscaling_group.worker.capacity_rebalance
    error_message = "Capacity rebalance replaces at-risk spot instances early."
  }

  assert {
    condition     = length(aws_autoscaling_group.worker.mixed_instances_policy[0].launch_template[0].override) == 5
    error_message = "Each allowed instance type should be an override."
  }
}

run "schedules_change_only_the_minimum" {
  command = plan

  # Datastores closed by default: the check warns, as it should.
  expect_failures = [check.workers_can_reach_the_datastores]

  assert {
    condition = alltrue([
      for s in [aws_autoscaling_schedule.daytime, aws_autoscaling_schedule.overnight] :
      s.max_size == -1 && s.desired_capacity == -1
    ])
    error_message = "A schedule that sets max_size or desired_capacity (the provider defaults both to 0) would empty the group under running jobs."
  }

  assert {
    condition     = aws_autoscaling_schedule.daytime.min_size == 1 && aws_autoscaling_schedule.overnight.min_size == 0
    error_message = "One warm worker by day, none overnight."
  }

  assert {
    condition     = aws_autoscaling_schedule.daytime.time_zone == "Asia/Shanghai"
    error_message = "The schedule is meant in the team's time, not UTC."
  }
}

run "scaling_reads_the_published_series_and_never_treats_silence_as_idle" {
  command = plan

  # Datastores closed by default: the check warns, as it should.
  expect_failures = [check.workers_can_reach_the_datastores]

  variables {
    environment = "prod-like"
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.jobs_waiting.namespace == "Flickpond"
      && aws_cloudwatch_metric_alarm.jobs_waiting.metric_name == "QueuedJobs"
      && aws_cloudwatch_metric_alarm.jobs_waiting.dimensions["Environment"] == "prod-like"
    )
    error_message = "Scale-out must read the totals series the publisher writes for this environment."
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.jobs_waiting.treat_missing_data == "notBreaching"
      && aws_cloudwatch_metric_alarm.workers_idle.treat_missing_data == "notBreaching"
    )
    error_message = "No data must neither add workers nor remove them."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.metrics_missing.treat_missing_data == "breaching"
    error_message = "A silent publisher has to raise its own alarm."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.workers_idle.evaluation_periods == 15 && aws_cloudwatch_metric_alarm.workers_idle.threshold == 0
    error_message = "Scale in only after 15 minutes with nothing queued or running."
  }
}

run "user_data_carries_the_image_and_no_secret" {
  command = plan

  # Datastores closed by default: the check warns, as it should.
  expect_failures = [check.workers_can_reach_the_datastores]

  assert {
    condition     = strcontains(base64decode(aws_launch_template.worker.user_data), "flickpond/worker:0123456789abcdef0123456789abcdef01234567")
    error_message = "Workers should boot the pinned image."
  }

  assert {
    condition     = strcontains(base64decode(aws_launch_template.worker.user_data), "REDIS_HOST=172.31.5.20")
    error_message = "Workers should reach Redis on the core host's private address."
  }

  assert {
    condition     = strcontains(base64decode(aws_launch_template.worker.user_data), "WORKER_HLS_MAX_HEIGHT=2160")
    error_message = "Worker settings should match the core host's."
  }

  assert {
    condition     = !strcontains(base64decode(aws_launch_template.worker.user_data), "\r")
    error_message = "CRLF in user data breaks bash (T-15)."
  }

  assert {
    condition     = aws_launch_template.worker.metadata_options[0].http_tokens == "required"
    error_message = "IMDSv2 only."
  }

  assert {
    condition     = output.parameter_path == "/flickpond/staging"
    error_message = "Secrets live under the environment's own path."
  }
}

run "datastores_stay_closed_unless_asked" {
  command = plan

  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.datastores_from_workers) == 0
    error_message = "Sprint 4 must not open Postgres or Redis on the core host."
  }

  # The check block warns about exactly this; a warning, not a failure.
  expect_failures = [check.workers_can_reach_the_datastores]
}

run "opening_the_datastores_admits_only_the_worker_group" {
  command = plan

  variables {
    open_datastores_to_workers = true
  }

  assert {
    condition = (
      length(aws_vpc_security_group_ingress_rule.datastores_from_workers) == 2
      && alltrue([for r in aws_vpc_security_group_ingress_rule.datastores_from_workers : r.security_group_id == "sg-0core000000000000" && r.cidr_ipv4 == null])
    )
    error_message = "Two rules on the base's group, from the worker group, never from an address range."
  }
}

run "a_floating_tag_is_rejected" {
  command = plan

  variables {
    worker_image               = "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/flickpond/worker:latest"
    open_datastores_to_workers = true # isolate the one failure under test
  }

  expect_failures = [var.worker_image]
}

run "an_x86_instance_type_is_rejected" {
  command = plan

  variables {
    instance_types             = ["c7g.2xlarge", "c7i.2xlarge"]
    open_datastores_to_workers = true
  }

  expect_failures = [var.instance_types]
}

run "a_daytime_minimum_above_the_maximum_is_rejected" {
  command = plan

  variables {
    daytime_min_workers        = 5
    max_workers                = 2
    open_datastores_to_workers = true
  }

  expect_failures = [aws_autoscaling_schedule.daytime]
}
