# Offline: both AWS providers are mocked, so these run with no account and no
# credentials (Terraform 1.7+):
#
#   terraform init -backend=false && terraform test
#
# They pin what would fail silently: an alarm that tells a topic in another
# region (CloudWatch drops it), a stopped health check that reads as healthy,
# a budget with nowhere to send, and an alert list nobody is on.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  mock_resource "aws_sns_topic" {
    defaults = {
      arn = "arn:aws:sns:ap-southeast-1:123456789012:flickpond-staging-alerts"
    }
  }
}

mock_provider "aws" {
  alias = "us_east_1"

  mock_resource "aws_sns_topic" {
    defaults = {
      arn = "arn:aws:sns:us-east-1:123456789012:flickpond-staging-site-health"
    }
  }
}

variables {
  alert_emails = ["a@example.com", "b@example.com"]
}

run "the_site_is_checked_as_a_visitor_reaches_it" {
  command = apply

  assert {
    condition = (
      aws_route53_health_check.site.fqdn == "flickpond.com"
      && aws_route53_health_check.site.port == 443
      && aws_route53_health_check.site.enable_sni
      && aws_route53_health_check.site.resource_path == "/api/health"
    )
    error_message = "The check should ask https://flickpond.com/api/health, with SNI, as a browser does."
  }

  assert {
    condition = (
      aws_route53_health_check.site.type == "HTTPS_STR_MATCH"
      && aws_route53_health_check.site.search_string == "\"status\":\"ok\""
    )
    error_message = "A 200 alone could be an nginx error page; the API's own answer should be required."
  }
}

run "the_site_down_alarm_tells_a_topic_in_its_own_region" {
  command = apply

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.site_down.alarm_actions == toset([aws_sns_topic.site_health.arn])
      && startswith(one(aws_cloudwatch_metric_alarm.site_down.alarm_actions), "arn:aws:sns:us-east-1:")
    )
    error_message = "Route 53's metrics are in us-east-1; an alarm there cannot notify a topic anywhere else."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.site_down.ok_actions == aws_cloudwatch_metric_alarm.site_down.alarm_actions
    error_message = "Recovery should reach the same people as the outage."
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.site_down.namespace == "AWS/Route53"
      && aws_cloudwatch_metric_alarm.site_down.dimensions["HealthCheckId"] == aws_route53_health_check.site.id
    )
    error_message = "The alarm should watch this health check."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.site_down.treat_missing_data == "breaching"
    error_message = "A check that stopped reporting must not read as a healthy site."
  }
}

run "the_budget_reaches_the_alert_topic" {
  command = apply

  assert {
    condition = (
      aws_budgets_budget.monthly.limit_amount == "80"
      && aws_budgets_budget.monthly.limit_unit == "USD"
      && aws_budgets_budget.monthly.time_unit == "MONTHLY"
    )
    error_message = "The plan's ceiling is US$80 a month."
  }

  assert {
    condition = (
      toset([for n in aws_budgets_budget.monthly.notification : n.notification_type]) == toset(["ACTUAL", "FORECASTED"])
      && alltrue([for n in aws_budgets_budget.monthly.notification : n.subscriber_sns_topic_arns == toset([aws_sns_topic.alerts.arn])])
    )
    error_message = "Both the spend and the forecast should be sent to the alert topic."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.alerts_topic.statement :
      anytrue([for p in s.principals : contains(p.identifiers, "budgets.amazonaws.com")])
    ])
    error_message = "Budgets refuses a budget whose topic it may not publish to."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.alerts_topic.statement :
      anytrue([for p in s.principals : contains(p.identifiers, "cloudwatch.amazonaws.com")])
    ])
    error_message = "The topic policy replaces the default one: without CloudWatch in it, the queue-metrics alarm goes nowhere."
  }
}

run "everyone_listed_is_subscribed_to_both_topics_by_email" {
  command = apply

  assert {
    condition = (
      toset([for s in aws_sns_topic_subscription.alerts_email : s.endpoint]) == toset(var.alert_emails)
      && toset([for s in aws_sns_topic_subscription.site_health_email : s.endpoint]) == toset(var.alert_emails)
    )
    error_message = "Each address should be on the alert topic and the site-health topic."
  }

  assert {
    condition = alltrue(concat(
      [for s in aws_sns_topic_subscription.alerts_email : s.protocol == "email"],
      [for s in aws_sns_topic_subscription.site_health_email : s.protocol == "email"],
    ))
    error_message = "Subscriptions should be email."
  }
}

run "an_alert_list_nobody_is_on_is_refused" {
  command = plan

  variables {
    alert_emails = []
  }

  expect_failures = [var.alert_emails]
}

run "an_entry_that_is_not_an_address_is_refused" {
  command = plan

  variables {
    alert_emails = ["ops team"]
  }

  expect_failures = [var.alert_emails]
}

run "fewer_than_three_checker_regions_is_refused" {
  command = plan

  variables {
    health_check_regions = ["ap-southeast-1", "ap-northeast-1"]
  }

  expect_failures = [var.health_check_regions]
}
