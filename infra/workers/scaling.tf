# Scaling on queue depth, published by app/metrics/queue_depth.py on the
# core host. The totals series (no Queue dimension) covers every queue a
# worker takes from, so a queue added later is scaled on without a change here.
#
# Why step scaling and not target tracking: target tracking on "backlog per
# worker" divides by the worker count, which is zero overnight -- the metric
# goes missing and the group can never scale out from nothing.

locals {
  queue_dimensions = {
    Environment = var.environment
  }
}

# Out: a job has waited two consecutive minutes. RQ workers take a job the
# moment they are free, so anything still queued means every worker is busy.
# Two datapoints rather than one, so a job that a worker picks up a second
# after a sample does not boot an instance for nothing.
resource "aws_cloudwatch_metric_alarm" "jobs_waiting" {
  alarm_name          = "flickpond-${var.environment}-jobs-waiting"
  alarm_description   = "Jobs are queued and every worker is busy: add workers."
  namespace           = var.metrics_namespace
  metric_name         = "QueuedJobs"
  dimensions          = local.queue_dimensions
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  # No data is not demand. A silent publisher is caught by metrics_missing.
  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_autoscaling_policy.scale_out.arn]
}

# Steps are measured from the threshold (1 queued job). While the alarm stays
# in ALARM the policy runs again once new instances have warmed up, so a
# backlog keeps adding workers until it drains or max_workers is reached.
resource "aws_autoscaling_policy" "scale_out" {
  name                      = "scale-out-on-queued-jobs"
  autoscaling_group_name    = aws_autoscaling_group.worker.name
  policy_type               = "StepScaling"
  adjustment_type           = "ChangeInCapacity"
  metric_aggregation_type   = "Maximum"
  estimated_instance_warmup = 180

  step_adjustment { # 1-4 queued
    metric_interval_lower_bound = 0
    metric_interval_upper_bound = 4
    scaling_adjustment          = 1
  }

  step_adjustment { # 5-14 queued
    metric_interval_lower_bound = 4
    metric_interval_upper_bound = 14
    scaling_adjustment          = 2
  }

  step_adjustment { # 15 or more
    metric_interval_lower_bound = 14
    scaling_adjustment          = 4
  }
}

# In: nothing queued and nothing running, anywhere, for 15 minutes. Only then,
# and straight to the scheduled minimum. Scaling in while any job runs could
# pick the instance running it, and an interrupted 4K encode costs far more
# than a quarter of an hour of an idle spot instance.
resource "aws_cloudwatch_metric_alarm" "workers_idle" {
  alarm_name          = "flickpond-${var.environment}-workers-idle"
  alarm_description   = "No job queued or running for 15 minutes: back to the scheduled minimum."
  evaluation_periods  = 15
  datapoints_to_alarm = 15
  comparison_operator = "LessThanOrEqualToThreshold"
  threshold           = 0
  # Missing data must not read as idle: that would scale in on a dead
  # publisher, possibly under running jobs.
  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_autoscaling_policy.scale_in.arn]

  metric_query {
    id          = "work"
    expression  = "queued + running"
    label       = "Jobs queued or running"
    return_data = true
  }

  metric_query {
    id = "queued"
    metric {
      namespace   = var.metrics_namespace
      metric_name = "QueuedJobs"
      dimensions  = local.queue_dimensions
      stat        = "Maximum"
      period      = 60
    }
  }

  metric_query {
    id = "running"
    metric {
      namespace   = var.metrics_namespace
      metric_name = "RunningJobs"
      dimensions  = local.queue_dimensions
      stat        = "Maximum"
      period      = 60
    }
  }
}

# ExactCapacity 0 is raised to the group's current minimum by AWS, so by day
# this leaves the warm worker and overnight it empties the group.
resource "aws_autoscaling_policy" "scale_in" {
  name                    = "scale-in-when-idle"
  autoscaling_group_name  = aws_autoscaling_group.worker.name
  policy_type             = "StepScaling"
  adjustment_type         = "ExactCapacity"
  metric_aggregation_type = "Maximum"

  step_adjustment {
    metric_interval_upper_bound = 0
    scaling_adjustment          = 0
  }
}

# The publisher's healthcheck only proves Redis answers, so it stays healthy
# while CloudWatch rejects every datapoint. This alarm is what notices: both
# scaling alarms read no data as "do nothing", which leaves the group frozen.
resource "aws_cloudwatch_metric_alarm" "metrics_missing" {
  alarm_name          = "flickpond-${var.environment}-queue-metrics-missing"
  alarm_description   = "No queue-depth datapoint for 5 minutes: autoscaling is blind. Check the queue-metrics service on the core host."
  namespace           = var.metrics_namespace
  metric_name         = "QueuedJobs"
  dimensions          = local.queue_dimensions
  statistic           = "SampleCount"
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = var.alarm_topic_arn == null ? [] : [var.alarm_topic_arn]
  ok_actions          = var.alarm_topic_arn == null ? [] : [var.alarm_topic_arn]
}
