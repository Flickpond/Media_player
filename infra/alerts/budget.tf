# The account's cost for the month, against a ceiling. The whole account
# rather than resources tagged Project=flickpond: a tag filter needs the tag
# activated for cost allocation first, and misses exactly the cost that
# matters most -- something nobody meant to create, and so nobody tagged.
#
# Budgets refreshes a few times a day, so this says "the money is going",
# not "it went a minute ago". The forecast warns before the spend does.

resource "aws_budgets_budget" "monthly" {
  name         = "flickpond-${var.environment}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    notification_type         = "ACTUAL"
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    subscriber_sns_topic_arns = [aws_sns_topic.alerts.arn]
  }

  notification {
    notification_type         = "FORECASTED"
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    subscriber_sns_topic_arns = [aws_sns_topic.alerts.arn]
  }

  # Budgets checks it may publish when the budget is saved, and refuses
  # the whole budget if the topic policy is not there yet.
  depends_on = [aws_sns_topic_policy.alerts]
}
