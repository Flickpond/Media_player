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

variable "app_role_name" {
  description = "The core host's role. This root adds what the core does about workers (publish queue depth, wake the group) as one inline policy; workers have a role of their own (iam.tf)."
  type        = string
}

variable "bucket_name" {
  type = string
}

variable "region" {
  type = string
}

# --- The managed datastores (contract 2.1) ----------------------------------
# Only the security groups and ports. The endpoints and credentials are read
# from Parameter Store at boot, like the core host does, so they are in
# neither this root's state nor the launch template.

variable "db_security_group_id" {
  description = "RDS PostgreSQL's security group. Workers are admitted to it by a rule this root adds."
  type        = string
}

variable "db_port" {
  type = number
}

variable "cache_security_group_id" {
  description = "ElastiCache Valkey's security group. Workers are admitted to it by a rule this root adds."
  type        = string
}

variable "cache_port" {
  type = number
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
    means fewer interruptions. All 4 vCPU: sprint 5 sized workers at
    c7g.xlarge once the ladder got cheaper to encode (30 fps cap, lower rungs
    from the 1080p rendition, audio encoded once). If the Thursday 4K timing
    misses the job timeout, set instance_types to the 2xlarge sizes in
    workers.tfvars.
  EOT
  type        = list(string)
  default     = ["c7g.xlarge", "c6g.xlarge", "c6gn.xlarge", "m7g.xlarge", "m6g.xlarge"]

  validation {
    condition     = length(var.instance_types) > 0 && alltrue([for t in var.instance_types : can(regex("^[a-z0-9]+g[a-z0-9]*\\.", t))])
    error_message = "Graviton (…g…) types only: the AMI and the image are arm64."
  }
}

variable "max_workers" {
  description = "The cost ceiling. Each is one 4 vCPU spot instance running one job at a time."
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

variable "worker_switches" {
  description = <<-EOT
    Feature switches for the final week, added on top of worker_settings
    rather than replacing it, e.g. { WORKER_PREVIEW_SPRITE = "true" }.
    Setting worker_settings itself replaces all four defaults above, which
    is how a switch quietly costs the 4K ladder. Empty changes nothing.
  EOT
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for key in keys(var.worker_switches) : can(regex("^[A-Z][A-Z0-9_]*$", key))])
    error_message = "Names are environment variables: upper case, digits and underscores."
  }

  # User data writes these into an unquoted heredoc: `$` or a backtick in a
  # value would be run by bash at boot.
  validation {
    condition     = alltrue([for value in values(var.worker_switches) : can(regex("^[A-Za-z0-9._:/-]*$", value))])
    error_message = "Values are plain words and numbers: no spaces, quotes, $ or backticks."
  }
}

variable "metrics_namespace" {
  description = "Must equal METRICS_NAMESPACE on the core host."
  type        = string
  default     = "Flickpond"
}

variable "alarm_topic_arn" {
  description = "SNS topic told when queue-depth data stops arriving, and again when it is back: infra/alerts' alerts_topic_arn. Null: the alarm still shows in the console, and tells nobody."
  type        = string
  default     = null
}

variable "open_datastores_to_workers" {
  description = <<-EOT
    Adds ingress from the worker security group to the RDS and ElastiCache
    security groups. Off by default: anything that opens a datastore is
    reviewed before it is applied, by someone other than whoever applies it
    (docs/datastore-review.md). Nothing on the core host changes either way.
  EOT
  type        = bool
  default     = false
}

variable "interruption_test" {
  description = "Creates the FIS experiment that sends one worker a spot interruption notice (fis.tf). On only for the test."
  type        = bool
  default     = false
}
