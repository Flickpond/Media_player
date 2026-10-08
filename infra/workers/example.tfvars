# Copy to workers.tfvars (gitignored) and fill in. The base's seven outputs
# come separately, generated into base.auto.tfvars.json -- see README.md.
environment     = "staging"
core_private_ip = "172.31.0.10" # terraform -chdir=../terraform state show aws_instance.app | grep private_ip
worker_image    = "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/flickpond/worker:0123456789abcdef0123456789abcdef01234567"
# alarm_topic_arn = "arn:aws:sns:ap-southeast-1:123456789012:flickpond-alerts"
