# Copy to staging.backend.hcl (gitignored) and fill in the bucket from
#   terraform -chdir=../bootstrap output -raw state_bucket
bucket       = "flickpond-tfstate-<account id>"
key          = "alerts/staging.tfstate"
region       = "ap-southeast-1"
encrypt      = true
use_lockfile = true
