# Cost guardrail.
#
# This deployment is meant to be cheap and short-lived, so an unnoticed
# runaway charge is the main financial risk. The alarm below emails as soon as
# estimated spend crosses the threshold.

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
  # AWS emails a confirmation link; the subscription is inactive until clicked.
}

# A CloudWatch alarm can only notify an SNS topic in its *own* region, and
# billing metrics exist solely in us-east-1 — so the billing alarm needs a
# topic there. The regional topic above serves every other alarm.
resource "aws_sns_topic" "billing_alerts" {
  provider = aws.us_east_1
  name     = "${local.name}-billing-alerts"
}

resource "aws_sns_topic_subscription" "billing_email" {
  provider  = aws.us_east_1
  topic_arn = aws_sns_topic.billing_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Billing metrics are only published in us-east-1, regardless of where the
# resources actually live — hence the aliased provider.
resource "aws_cloudwatch_metric_alarm" "billing" {
  provider = aws.us_east_1

  alarm_name          = "${local.name}-estimated-charges"
  alarm_description   = "Estimated AWS charges exceeded $${var.monthly_budget_usd}"
  namespace           = "AWS/Billing"
  metric_name         = "EstimatedCharges"
  dimensions          = { Currency = "USD" }
  statistic           = "Maximum"
  period              = 21600 # 6h — billing metrics update roughly that often
  evaluation_periods  = 1
  threshold           = var.monthly_budget_usd
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.billing_alerts.arn]
}

# A second signal: if the instance stops responding, the site is down.
resource "aws_cloudwatch_metric_alarm" "instance_health" {
  alarm_name          = "${local.name}-instance-unhealthy"
  alarm_description   = "EC2 status checks failing — the application is likely down"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  dimensions          = { InstanceId = aws_instance.app.id }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}
