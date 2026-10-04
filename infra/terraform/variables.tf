variable "region" {
  description = "Singapore: the nearest region to the team that needs no ICP filing, like the current Hong Kong server."
  type        = string
  default     = "ap-southeast-1"
}

variable "environment" {
  description = "Names every resource, so staging and production can live in one account."
  type        = string
  default     = "staging"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.environment))
    error_message = "Lower-case letters, digits and hyphens; it ends up in a bucket name."
  }
}

variable "instance_type" {
  description = <<-EOT
    Graviton, compute-optimised. 8 vCPU / 16 GiB is enough to encode a 4K
    ladder inside the job timeout with the API, reaper and two workers on the
    same host. The image is multi-arch, so arm64 needs no code change.
  EOT
  type        = string
  default     = "c7g.2xlarge"

  validation {
    condition     = can(regex("^[a-z0-9]+g[a-z0-9]*\\.", var.instance_type))
    error_message = "The AMI lookup is arm64; pick a Graviton (…g…) instance family."
  }
}

variable "root_volume_gb" {
  description = "Scratch space for transcodes. Videos themselves live in S3."
  type        = number
  default     = 50
}

variable "admin_cidrs" {
  description = <<-EOT
    Who may SSH in, e.g. ["203.0.113.7/32"]. Empty means no SSH at all: the
    instance is reachable through SSM Session Manager regardless.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.admin_cidrs : cidr != "0.0.0.0/0"])
    error_message = "SSH open to the internet is exactly what this variable exists to avoid."
  }
}

variable "ssh_key_name" {
  description = "An existing EC2 key pair, or null to rely on Session Manager."
  type        = string
  default     = null
}

variable "site_origins" {
  description = <<-EOT
    Origins allowed to PUT parts straight to the bucket (direct upload) and to
    GET segments through hls.js. Exactly the page's origin -- a wildcard here
    lets any site spend this bucket's bandwidth with a user's signed URL.
  EOT
  type        = list(string)
  default     = ["https://flickpond.com"]

  validation {
    condition     = alltrue([for origin in var.site_origins : origin != "*"])
    error_message = "List the site's origins explicitly."
  }
}
