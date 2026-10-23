# Where every alert goes. Email, because it needs no app, no account and no
# on-call rota: a five-person team reads its mail. Subscriptions start as
# "PendingConfirmation" and deliver nothing until the link AWS sends is
# clicked -- Terraform cannot click it.
#
# Unencrypted on purpose: CloudWatch and Budgets cannot publish to a topic
# encrypted with the AWS-managed SNS key, and an alert carries an alarm name
# and a number, never a secret.

data "aws_caller_identity" "current" {}

# The queue-metrics alarm (infra/workers, `alarm_topic_arn`) and the budget.
resource "aws_sns_topic" "alerts" {
  name = "flickpond-${var.environment}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  for_each = toset(var.alert_emails)

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

# A topic policy replaces the default one, so CloudWatch is named as well as
# Budgets. Each only for this account's alarms and budgets.
data "aws_iam_policy_document" "alerts_topic" {
  statement {
    sid       = "CloudWatchAlarms"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid       = "Budgets"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:budgets::${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts_topic.json
}

# The site-down alarm's topic, in us-east-1 with the alarm (versions.tf).
# The default policy lets this account's alarms publish; nothing else does.
resource "aws_sns_topic" "site_health" {
  provider = aws.us_east_1

  name = "flickpond-${var.environment}-site-health"
}

resource "aws_sns_topic_subscription" "site_health_email" {
  provider = aws.us_east_1
  for_each = toset(var.alert_emails)

  topic_arn = aws_sns_topic.site_health.arn
  protocol  = "email"
  endpoint  = each.value
}
