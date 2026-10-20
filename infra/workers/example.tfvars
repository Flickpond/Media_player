# Copy to workers.tfvars (gitignored) and fill in. The base's outputs come
# separately, generated into base.auto.tfvars.json -- see README.md.
environment  = "staging"
worker_image = "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/flickpond/worker:0123456789abcdef0123456789abcdef01234567"
# Only after docs/datastore-review.md has been read by someone other than
# whoever applies (sprint 5 rule 7).
# open_datastores_to_workers = true
# alarm_topic_arn = "arn:aws:sns:ap-southeast-1:123456789012:flickpond-staging-alerts"
#   from: terraform -chdir=../alerts output -raw alerts_topic_arn
# Final-week switches, added to the worker settings rather than replacing them:
# worker_switches = { WORKER_PREVIEW_SPRITE = "true" }
