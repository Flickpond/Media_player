# --- From the base (infra/terraform outputs.tf), names unchanged ------------
# Generate these rather than typing them; see README.md.

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  description = "Workers spread across these zones, so one zone running out of spot capacity does not stop the group."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) > 0
    error_message = "At least one subnet."
  }
}

variable "security_group_id" {
  description = "The core host's security group: workers reach Postgres and Redis through it."
  type        = string
}

variable "instance_profile_name" {
  description = "Workers need exactly the bucket access the core host has."
  type        = string
}

variable "app_role_name" {
  description = "The role behind instance_profile_name. This root attaches its extra permissions here, as a separate policy."
  type        = string
}

variable "bucket_name" {
  type = string
}

variable "region" {
  type = string
}

# --- Not in the base outputs yet ---------------------------------------------

variable "core_private_ip" {
  description = <<-EOT
    The core host's private address, where workers reach Postgres and Redis
    in the sprint 4 shape. Becomes a base output (#62); until then:
      terraform -chdir=../terraform state show aws_instance.app | grep private_ip
    Replaced by the RDS and ElastiCache endpoints when the datastores move.
  EOT
  type        = string

  validation {
    condition     = can(cidrhost("${var.core_private_ip}/32", 0))
    error_message = "An IPv4 address, e.g. 172.31.5.20."
  }
}

# --- This root's own settings ------------------------------------------------

variable "environment" {
  description = <<-EOT
    Names every resource, and must equal METRICS_ENVIRONMENT on the core host:
    the alarms read the queue-depth series with this Environment dimension,
    and a mismatch means they watch a series nobody publishes.
  EOT
  type        = string
  default     = "staging"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.environment))
    error_message = "Lower-case letters, digits and hyphens, like the base."
  }
}

variable "worker_image" {
  description = <<-EOT
    The image to boot, pinned to a full commit SHA:
    <account>.dkr.ecr.<region>.amazonaws.com/flickpond/worker:<40-hex SHA>.
    infra/registry creates the repository; CI pushes one image per commit.
  EOT
  type        = string

  validation {
    # A tag like `latest` would make two workers in one group run different
    # code depending on when each booted.
    condition     = can(regex("^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/flickpond/worker:[0-9a-f]{40}$", var.worker_image))
    error_message = "Must be the ECR worker repository, tagged with a full 40-character commit SHA."
  }
}

variable "instance_types" {
  description = <<-EOT
    Graviton types the group may use, in no particular order: spot draws from
    whichever pool is cheapest and least likely to be reclaimed, so more types
    means fewer interruptions. All 8 vCPU, the size A's timing showed encodes
    a 4K ladder inside the job timeout.
  EOT
  type        = list(string)
  default     = ["c7g.2xlarge", "c6g.2xlarge", "c6gn.2xlarge", "m7g.2xlarge", "m6g.2xlarge"]

  validation {
    condition     = length(var.instance_types) > 0 && alltrue([for t in var.instance_types : can(regex("^[a-z0-9]+g[a-z0-9]*\\.", t))])
    error_message = "Graviton (…g…) types only: the AMI and the image are arm64."
  }
}

variable "max_workers" {
  description = "The cost ceiling. Each is one 8 vCPU spot instance running one job at a time."
  type        = number
  default     = 4

  validation {
    condition     = var.max_workers >= 1 && var.max_workers <= 20
    error_message = "Between 1 and 20."
  }
}

variable "daytime_min_workers" {
  description = "Workers kept warm in working and demo hours, so the first upload does not wait for a boot."
  type        = number
  default     = 1

  validation {
    condition     = var.daytime_min_workers >= 0
    error_message = "Zero or more."
  }
}

variable "schedule_time_zone" {
  description = "IANA zone the two schedules below are read in."
  type        = string
  default     = "Asia/Shanghai"
}

variable "daytime_starts" {
  description = "Cron: when the daytime minimum starts."
  type        = string
  default     = "0 8 * * *"
}

variable "overnight_starts" {
  description = "Cron: when the minimum drops to zero. Running jobs finish; idle workers then scale in."
  type        = string
  default     = "0 0 * * *"
}

variable "root_volume_gb" {
  description = "Scratch for one transcode: source, MP4 and ladder of a 4K upload, plus the image."
  type        = number
  default     = 50
}

variable "worker_settings" {
  description = <<-EOT
    Worker settings, the same as deploy/aws/compose.aws.yml gives the core
    host's workers -- a job must not depend on which host took it.
  EOT
  type        = map(string)
  default = {
    WORKER_HLS_MAX_HEIGHT         = "2160"
    WORKER_FFMPEG_MAX_HEIGHT      = "1080"
    WORKER_FFMPEG_TIMEOUT_SECONDS = "1770"
    WORKER_JOB_TIMEOUT_SECONDS    = "1800"
  }
}

variable "metrics_namespace" {
  description = "Must equal METRICS_NAMESPACE on the core host."
  type        = string
  default     = "Flickpond"
}

variable "alarm_topic_arn" {
  description = "Optional SNS topic told when queue-depth data stops arriving. Null: the alarm still shows in the console."
  type        = string
  default     = null
}

variable "open_datastores_to_workers" {
  description = <<-EOT
    Adds Postgres and Redis ingress from the worker security group to the
    datastores' security group. Off in sprint 4 (plan only). Today that group
    is the base's app group; after #62 it is the RDS and ElastiCache groups,
    so turning this on no longer needs any change on the core host.
  EOT
  type        = bool
  default     = false
}
