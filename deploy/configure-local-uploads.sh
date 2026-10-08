#!/bin/sh
set -eu
# The mc alias is supplied by the operator; no credentials are stored here.
# MinIO configures browser origins at the server level, not S3 bucket CORS.
alias_name=${1:-local}
mc admin config set "$alias_name" api cors_allow_origin="http://localhost:3000,http://127.0.0.1:3000" stale_uploads_expiry=48h
mc admin service restart "$alias_name"
