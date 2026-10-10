# Copy to staging.backend.hcl (gitignored) and fill in the bucket from
#   terraform -chdir=../bootstrap output -raw state_bucket
# For production, the same with key = "base/production.tfstate".
bucket       = "flickpond-tfstate-<account id>"
key          = "base/staging.tfstate"
region       = "ap-southeast-1"
encrypt      = true
use_lockfile = true
