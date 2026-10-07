variable "region" {
  description = "Same region as the base (infra/terraform), so workers pull without cross-region transfer."
  type        = string
  default     = "ap-southeast-1"
}

variable "github_repository" {
  description = "The only repository whose workflows may assume the push role."
  type        = string
  default     = "Flickpond/Media_player"
}

variable "push_branch" {
  description = "The only branch whose workflow runs may push. Pull requests never can."
  type        = string
  default     = "main"
}

variable "create_github_oidc_provider" {
  description = <<-EOT
    An account holds at most one OIDC provider for GitHub Actions. True creates
    it here; false uses the one that already exists (e.g. if another stack
    made it first). `aws iam list-open-id-connect-providers` shows which.
  EOT
  type        = bool
  default     = true
}

variable "images_to_keep" {
  description = "Newest images kept. Older ones expire, so keep this above how far back a rollback could reach."
  type        = number
  default     = 30

  validation {
    condition     = var.images_to_keep >= 5
    error_message = "Keep at least a few images to roll back to."
  }
}
