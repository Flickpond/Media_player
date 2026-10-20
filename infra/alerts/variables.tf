variable "region" {
  description = "Where the site, its queue metrics and the main alert topic are."
  type        = string
  default     = "ap-southeast-1"
}

variable "environment" {
  description = <<-EOT
    Names every resource. Production still runs the stack proven as staging,
    so this stays "staging": it must match the workers root, whose
    queue-metrics alarm sends to the topic made here.
  EOT
  type        = string
  default     = "staging"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.environment))
    error_message = "Lower-case letters, digits and hyphens, like the other roots."
  }
}

variable "alert_emails" {
  description = <<-EOT
    Who is told. Each address gets one confirmation email per topic (two in
    all) and hears nothing until both links are clicked. Set in the
    gitignored alerts.tfvars: addresses are personal data, not code.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.alert_emails) > 0
    error_message = "An alert nobody receives is the problem this root exists to fix."
  }

  validation {
    condition     = alltrue([for email in var.alert_emails : can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", email))])
    error_message = "Each entry must be an email address."
  }
}

variable "site_domain" {
  description = "Checked over HTTPS from outside AWS's network, the way a visitor reaches it."
  type        = string
  default     = "flickpond.com"
}

variable "health_path" {
  description = "Answered by the API through nginx, so a pass proves both are up."
  type        = string
  default     = "/api/health"
}

variable "health_check_regions" {
  description = "Where Route 53 checks from. Its minimum is three; the nearest three to the users."
  type        = list(string)
  default     = ["ap-southeast-1", "ap-northeast-1", "ap-southeast-2"]

  validation {
    condition     = length(var.health_check_regions) >= 3
    error_message = "Route 53 needs at least three checker regions."
  }
}

variable "monthly_budget_usd" {
  description = "The whole account's monthly cost. Told when it is spent, and when AWS forecasts it will be."
  type        = number
  default     = 80

  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "A budget of nothing would alert from the first cent."
  }
}
