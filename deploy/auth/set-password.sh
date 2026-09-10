#!/bin/sh
# Create or rotate the access gate's credentials.
#
#   ./deploy/auth/set-password.sh                 # random password, user "flickpond"
#   ./deploy/auth/set-password.sh alice           # random password, user "alice"
#   ./deploy/auth/set-password.sh alice 'secret'  # explicit password
#
# Run this instead of writing htpasswd by hand. Doing it by hand is how the
# gate ends up chmod 600, which fails in a way that looks like it is working --
# see "The 600 trap" in README.md.
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
USERNAME=${1:-flickpond}
PASSWORD=${2:-}

if [ -z "$PASSWORD" ]; then
    PASSWORD=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | cut -c1-16)
    GENERATED=yes
else
    GENERATED=no
fi

# apr1 is the format nginx supports everywhere. openssl is present on the hosts
# we deploy to; the httpd image is the fallback for machines without it.
if command -v openssl >/dev/null 2>&1; then
    HASH=$(openssl passwd -apr1 "$PASSWORD")
elif command -v docker >/dev/null 2>&1; then
    HASH=$(docker run --rm httpd:2.4-alpine htpasswd -nbB "$USERNAME" "$PASSWORD" | cut -d: -f2-)
else
    echo "error: need openssl or docker to hash the password" >&2
    exit 1
fi

printf '%s:%s\n' "$USERNAME" "$HASH" > "$DIR/htpasswd"

# 644, deliberately. nginx runs its master as root but its workers as `nginx`,
# and a worker is what opens auth_basic_user_file. A 600 file is unreadable to
# that worker, which produces a 500 for anyone who supplies correct credentials
# while unauthenticated probes still get a clean 401 -- so the gate looks
# healthy until someone tries to log in. The file holds an apr1 hash, not a
# plaintext password, and the host should already be root-access-only.
chmod 644 "$DIR/htpasswd"

cat > "$DIR/auth.conf" <<'CONF'
auth_basic "Flickpond";
auth_basic_user_file /etc/nginx/app-auth/htpasswd;
CONF
chmod 644 "$DIR/auth.conf"

echo "wrote $DIR/htpasswd and $DIR/auth.conf (mode 644)"
echo "  username: $USERNAME"
if [ "$GENERATED" = yes ]; then
    echo "  password: $PASSWORD"
    echo "  (generated -- save it now, it is not stored anywhere in plaintext)"
fi

CONTAINER=$(docker ps --filter "name=frontend" --format '{{.Names}}' 2>/dev/null | head -1 || true)
if [ -n "${CONTAINER:-}" ]; then
    docker exec "$CONTAINER" nginx -s reload >/dev/null 2>&1 && echo "reloaded nginx in $CONTAINER"
    sleep 1
    echo
    "$DIR/verify.sh" "http://127.0.0.1" "$USERNAME" "$PASSWORD" || true
else
    echo "no running frontend container found -- start the stack, then run verify.sh"
fi
