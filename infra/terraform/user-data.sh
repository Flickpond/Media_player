#!/bin/bash
# First boot only: the tools the deploy steps assume. The application itself
# is deployed by hand, exactly as on the current server (see the cutover
# runbook) -- user data that clones and starts the app would need the repo's
# credentials baked into the instance.
set -euo pipefail

dnf install -y docker git
systemctl enable --now docker

# Compose v2 as a Docker CLI plugin, matching `docker compose` in CLAUDE.md.
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
  "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-aarch64"
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

usermod -aG docker ec2-user
