#!/bin/sh
# Prove the gate actually gates.
#
#   ./deploy/auth/verify.sh [base-url] [username] [password]
#
# Checks the three things that matter and, crucially, distinguishes a working
# gate from the 500 that a wrongly-permissioned htpasswd produces.
set -eu

BASE=${1:-http://127.0.0.1}
USERNAME=${2:-flickpond}
PASSWORD=${3:-}

code() { curl -s -o /dev/null -m 10 -w '%{http_code}' "$@"; }

FAIL=0

ANON=$(code "$BASE/api/jobs")
if [ "$ANON" = "401" ]; then
    echo "  ok    anonymous /api/jobs -> 401"
else
    echo "  FAIL  anonymous /api/jobs -> $ANON (expected 401; the API is exposed)"
    FAIL=1
fi

HEALTH=$(code "$BASE/healthz")
if [ "$HEALTH" = "200" ]; then
    echo "  ok    /healthz -> 200 (auth-exempt, container healthcheck works)"
else
    echo "  FAIL  /healthz -> $HEALTH (expected 200; the healthcheck will fail)"
    FAIL=1
fi

if [ -n "$PASSWORD" ]; then
    AUTHED=$(code -u "$USERNAME:$PASSWORD" "$BASE/api/jobs")
    case "$AUTHED" in
        200)
            echo "  ok    authenticated /api/jobs -> 200"
            ;;
        500)
            echo "  FAIL  authenticated /api/jobs -> 500"
            echo
            echo "        This is the htpasswd permission trap. nginx workers run as the"
            echo "        'nginx' user and cannot read a 600 file, so correct credentials"
            echo "        produce a 500 while anonymous probes still get a tidy 401."
            echo "        Confirm with:  docker logs <frontend> 2>&1 | grep 'Permission denied'"
            echo "        Fix with:      chmod 644 deploy/auth/htpasswd && docker exec <frontend> nginx -s reload"
            FAIL=1
            ;;
        401)
            echo "  FAIL  authenticated /api/jobs -> 401 (wrong password, or htpasswd not reloaded)"
            FAIL=1
            ;;
        *)
            echo "  FAIL  authenticated /api/jobs -> $AUTHED"
            FAIL=1
            ;;
    esac
else
    echo "  skip  authenticated check (no password given)"
fi

[ "$FAIL" = 0 ] && echo "gate verified" || { echo "gate NOT correct"; exit 1; }
