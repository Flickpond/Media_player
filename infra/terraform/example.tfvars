# Copy to staging.tfvars (gitignored) and fill in.
environment  = "staging"
admin_cidrs  = ["203.0.113.7/32"] # your IP; leave [] to use Session Manager only
ssh_key_name = null
site_origins = ["https://staging.flickpond.com"]
