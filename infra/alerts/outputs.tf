output "alerts_topic_arn" {
  description = "alarm_topic_arn in infra/workers' workers.tfvars: the queue-metrics alarm sends here."
  value       = aws_sns_topic.alerts.arn
}

output "site_health_topic_arn" {
  description = "In us-east-1, with the site-down alarm."
  value       = aws_sns_topic.site_health.arn
}

output "health_check_id" {
  description = "aws route53 get-health-check-status --health-check-id <this>"
  value       = aws_route53_health_check.site.id
}

output "alarm_names" {
  description = "aws cloudwatch set-alarm-state --alarm-name <this> ... to test delivery (README.md)."
  value = {
    site_down = aws_cloudwatch_metric_alarm.site_down.alarm_name
  }
}
