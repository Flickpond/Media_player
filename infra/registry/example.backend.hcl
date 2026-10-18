# Copy to registry.backend.hcl (gitignored) and fill in the bucket from
#   terraform -chdir=../bootstrap output -raw state_bucket
bucket       = "flickpond-tfstate-<account id>"
key          = "registry/terraform.tfstate"
region       = "ap-southeast-1"
encrypt      = true
use_lockfile = true
