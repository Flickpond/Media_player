#!/usr/bin/env bash
# Write this host's datastore settings into .env from SSM Parameter Store.
#
#   deploy/aws/fetch-secrets.sh staging
#
# Run on an AWS host, as part of every deploy: the instance role can read
# /flickpond/<environment>/ and nothing else, and the values never pass
# through a terminal, a chat or the repo. Prints the names it set, never the
# values. Re-running after `terraform apply -replace=random_password.db`
# (a rotation) picks up the new password; restart the services after.
set -euo pipefail

environment="${1:?usage: fetch-secrets.sh <environment, e.g. staging>}"
region="${AWS_REGION:-ap-southeast-1}"

case "$environment" in
  *[!a-z0-9-]* | "")
    echo "environment must contain only lowercase letters, digits and hyphens" >&2
    exit 2
    ;;
esac

command -v aws >/dev/null || { echo "aws CLI is required" >&2; exit 127; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 127; }

cd "$(dirname "$0")/../.."

param() {
  aws ssm get-parameter --region "$region" --with-decryption \
    --name "/flickpond/${environment}/$1" --query Parameter.Value --output text
}

# Python rather than sed: the DSN is full of the characters sed treats as
# delimiters, and a half-replaced DSN fails in confusing ways.
postgres_dsn="$(param postgres_dsn)"
redis_host="$(param redis_host)"
redis_password="$(param redis_password)"

POSTGRES_DSN="$postgres_dsn" \
REDIS_HOST="$redis_host" \
REDIS_PASSWORD="$redis_password" \
python3 - <<'PY'
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

wanted = {
    "POSTGRES_DSN": os.environ["POSTGRES_DSN"],
    "REDIS_HOST": os.environ["REDIS_HOST"],
    "REDIS_PORT": "6379",
    "REDIS_SSL": "true",
    "REDIS_PASSWORD": os.environ["REDIS_PASSWORD"],
}
path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
kept = [line for line in lines if line.split("=", 1)[0] not in wanted]
kept += [f"{key}={value}" for key, value in wanted.items()]

# All three SSM values are single-line by construction. Reject an unexpected
# value before replacing a working configuration with a malformed dotenv file.
if any("\n" in value or "\r" in value for value in wanted.values()):
    raise SystemExit("an SSM parameter unexpectedly contains a newline")

with NamedTemporaryFile(
    "w", encoding="utf-8", dir=path.parent, prefix=".env.", delete=False
) as output:
    output.write("\n".join(kept) + "\n")
    temporary = Path(output.name)
temporary.chmod(0o600)
temporary.replace(path)
print("set in .env:", ", ".join(wanted))
PY
