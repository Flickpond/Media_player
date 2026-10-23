# Is flickpond.com up, as a visitor sees it? Route 53 asks from three regions
# every 30 seconds, over HTTPS with SNI, for the API's health route through
# nginx. Passing needs the body as well as a 200: an nginx error page served
# with 200 is not the API answering.
#
# Timing, so a deploy does not page anyone: a checker calls the site down
# after three failures in a row (90 s), and the alarm wants two minutes of
# that. A `docker compose up -d --build` restarts the API in well under
# three and a half minutes; an outage that long is worth an email.

resource "aws_route53_health_check" "site" {
  fqdn              = var.site_domain
  type              = "HTTPS_STR_MATCH"
  port              = 443
  resource_path     = var.health_path
  search_string     = "\"status\":\"ok\""
  enable_sni        = true
  request_interval  = 30
  failure_threshold = 3
  regions           = var.health_check_regions

  tags = {
    Name = "flickpond-${var.environment}-site"
  }
}

resource "aws_cloudwatch_metric_alarm" "site_down" {
  provider = aws.us_east_1

  alarm_name          = "flickpond-${var.environment}-site-down"
  alarm_description   = "https://${var.site_domain}${var.health_path} is failing from Route 53's checkers. Check the core host: docker compose ps, then the api and nginx logs."
  namespace           = "AWS/Route53"
  metric_name         = "HealthCheckStatus"
  dimensions          = { HealthCheckId = aws_route53_health_check.site.id }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  # No data means the check itself has stopped; silence would look healthy.
  treat_missing_data = "breaching"
  alarm_actions      = [aws_sns_topic.site_health.arn]
  # Recovery is news too: it says nobody needs to log in.
  ok_actions = [aws_sns_topic.site_health.arn]
}
