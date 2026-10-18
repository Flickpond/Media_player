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
  vpc_id                  = "vpc-0123456789abcdef0"
  subnet_ids              = ["subnet-0aaaaaaaaaaaaaaaa", "subnet-0bbbbbbbbbbbbbbbb"]
  app_role_name           = "flickpond-staging-app"
  bucket_name             = "flickpond-staging-videos-abcd1234"
  region                  = "ap-southeast-1"
  db_security_group_id    = "sg-0db0000000000000"
  db_port                 = 5432
  cache_security_group_id = "sg-0cache000000000"
  cache_port              = 6379
  worker_image            = "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/flickpond/worker:0123456789abcdef0123456789abcdef01234567"
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

  assert {
    condition = alltrue([
      for o in aws_autoscaling_group.worker.mixed_instances_policy[0].launch_template[0].override :
      endswith(o.instance_type, ".xlarge")
    ])
    error_message = "Sprint 5 sized workers at 4 vCPU (c7g.xlarge); larger is a tfvars decision, not a default."
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
    condition = alltrue([
      for name in ["postgres_dsn", "redis_host", "redis_password"] :
      strcontains(base64decode(aws_launch_template.worker.user_data), "param ${name})")
    ])
    error_message = "Workers should read the same three parameters the base writes for the core host."
  }

  assert {
    condition     = strcontains(base64decode(aws_launch_template.worker.user_data), "REDIS_SSL=true")
    error_message = "ElastiCache has in-transit encryption on: a plain-text client cannot connect."
  }

  assert {
    condition = (
      strcontains(base64decode(aws_launch_template.worker.user_data), "POSTGRES_DSN=$postgres_dsn")
      && strcontains(base64decode(aws_launch_template.worker.user_data), "REDIS_HOST=$redis_host")
    )
    error_message = "Addresses come from Parameter Store at boot, not from the launch template."
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

run "a_stopping_worker_gets_100_seconds_to_give_its_job_back" {
  command = plan

  # Datastores closed by default: the check warns, as it should.
  expect_failures = [check.workers_can_reach_the_datastores]

  assert {
    condition = alltrue([
      for s in [
        "spot/instance-action",
        "autoscaling/target-lifecycle-state",
        "docker stop --time 100 worker",
        "--stop-timeout 100",
        "GROUP_NAME=flickpond-staging-workers",
        "HOOK_NAME=drain-worker",
        "systemctl enable --now flickpond-drain.service",
      ] : strcontains(base64decode(aws_launch_template.worker.user_data), s)
    ])
    error_message = "Contract 2.3: a spot notice or a scale-in runs docker stop --time 100 on the worker, from a service started at boot."
  }

  assert {
    condition = (
      one(aws_autoscaling_group.worker.initial_lifecycle_hook).name == "drain-worker"
      && one(aws_autoscaling_group.worker.initial_lifecycle_hook).lifecycle_transition == "autoscaling:EC2_INSTANCE_TERMINATING"
      && one(aws_autoscaling_group.worker.initial_lifecycle_hook).default_result == "CONTINUE"
      && one(aws_autoscaling_group.worker.initial_lifecycle_hook).heartbeat_timeout >= 105
    )
    error_message = "A scale-in must wait for the 5 s poll and the 100 s stop, and terminate anyway if the drain never answers."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.worker.statement :
      s.sid == "FinishDrainingThisGroup" && s.actions == toset(["autoscaling:CompleteLifecycleAction"]) && length(s.resources) == 1
    ])
    error_message = "Completing the lifecycle action is allowed on this group only."
  }
}

run "datastores_stay_closed_unless_asked" {
  command = plan

  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.datastores_from_workers) == 0
    error_message = "RDS and ElastiCache must not be opened until the review has been read and the flag set."
  }

  # The check block warns about exactly this; a warning, not a failure.
  expect_failures = [check.workers_can_reach_the_datastores]
}

run "workers_reach_each_datastore_only_through_its_own_group" {
  command = plan

  # Datastores closed by default: the check warns, as it should.
  expect_failures = [check.workers_can_reach_the_datastores]

  assert {
    condition = (
      length(aws_vpc_security_group_egress_rule.datastores) == 2
      && aws_vpc_security_group_egress_rule.datastores["Postgres"].referenced_security_group_id == "sg-0db0000000000000"
      && aws_vpc_security_group_egress_rule.datastores["Postgres"].from_port == 5432
      && aws_vpc_security_group_egress_rule.datastores["Valkey"].referenced_security_group_id == "sg-0cache000000000"
      && aws_vpc_security_group_egress_rule.datastores["Valkey"].from_port == 6379
    )
    error_message = "Postgres egress goes to the RDS group on db_port, Valkey to the ElastiCache group on cache_port."
  }
}

run "opening_the_datastores_admits_only_the_worker_group" {
  command = plan

  variables {
    open_datastores_to_workers = true
  }

  assert {
    condition = (
      length(aws_vpc_security_group_ingress_rule.datastores_from_workers) == 2
      && aws_vpc_security_group_ingress_rule.datastores_from_workers["Postgres"].security_group_id == "sg-0db0000000000000"
      && aws_vpc_security_group_ingress_rule.datastores_from_workers["Postgres"].from_port == 5432
      && aws_vpc_security_group_ingress_rule.datastores_from_workers["Valkey"].security_group_id == "sg-0cache000000000"
      && aws_vpc_security_group_ingress_rule.datastores_from_workers["Valkey"].from_port == 6379
      && alltrue([for r in aws_vpc_security_group_ingress_rule.datastores_from_workers : r.cidr_ipv4 == null && r.cidr_ipv6 == null])
    )
    error_message = "One rule on the RDS group and one on the ElastiCache group, from the worker group, never from an address range."
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

run "no_interruption_experiment_exists_unless_asked" {
  command = plan

  # Datastores closed by default: the check warns, as it should.
  expect_failures = [check.workers_can_reach_the_datastores]

  assert {
    condition = (
      length(aws_fis_experiment_template.interruption_test) == 0
      && length(aws_iam_role.interruption_test) == 0
    )
    error_message = "A role that may interrupt workers should exist only while the test does."
  }
}

run "the_interruption_experiment_warns_one_worker_of_this_group_two_minutes_out" {
  command = plan

  variables {
    open_datastores_to_workers = true
    interruption_test          = true
  }

  assert {
    condition = (
      one(aws_fis_experiment_template.interruption_test[0].action).action_id == "aws:ec2:send-spot-instance-interruptions"
      && one(one(aws_fis_experiment_template.interruption_test[0].action).parameter).value == "PT2M"
    )
    error_message = "The experiment should send the same two-minute notice a real reclaim does."
  }

  assert {
    condition = (
      one(aws_fis_experiment_template.interruption_test[0].target).selection_mode == "COUNT(1)"
      && one(one(aws_fis_experiment_template.interruption_test[0].target).resource_tag).value == "flickpond-staging-workers"
    )
    error_message = "One running worker of this environment's group, never any other instance."
  }

  assert {
    condition     = strcontains(aws_iam_role_policy.interruption_test[0].policy, "\"ec2:ResourceTag/aws:autoscaling:groupName\":\"flickpond-staging-workers\"")
    error_message = "The experiment's role may interrupt only this group's instances."
  }
}

run "workers_have_a_role_of_their_own_and_boot_fast" {
  command = plan

  expect_failures = [check.workers_can_reach_the_datastores]

  assert {
    condition     = aws_iam_role.worker.name == "flickpond-staging-worker" && aws_iam_instance_profile.worker.role == aws_iam_role.worker.name
    error_message = "Workers should have their own role and instance profile."
  }

  assert {
    condition     = aws_launch_template.worker.iam_instance_profile[0].name == aws_iam_instance_profile.worker.name
    error_message = "The launch template should use the workers' own profile, not the core's."
  }

  assert {
    condition     = aws_iam_role_policy.workers.role == "flickpond-staging-app" && aws_iam_role_policy.worker.name == "worker"
    error_message = "The core keeps one inline policy from here; the workers have a policy of their own."
  }

  assert {
    condition = alltrue([
      for snippet in ["command -v docker >/dev/null || dnf install -y docker", "systemctl disable --now ecs"] :
      strcontains(base64decode(aws_launch_template.worker.user_data), snippet)
    ])
    error_message = "User data should install only what the image lacks, and stop the ECS agent."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.worker.statement :
      length(s.resources) == 3 && alltrue([for r in s.resources : !endswith(r, "*")])
      if s.sid == "ReadTheConnectionSettings"
    ])
    error_message = "Workers read exactly the three connection settings, never the whole parameter path (the CloudFront key lives there)."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.core.statement :
      s.sid == "WakeThisGroup" && s.actions == toset(["autoscaling:SetDesiredCapacity"]) && length(s.resources) == 1
    ])
    error_message = "The core may wake this group and no other."
  }
}
