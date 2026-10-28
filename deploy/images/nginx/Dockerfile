# The public web server. Built rather than pulled: every upstream nginx tag,
# even the newest, ships Alpine packages with fixes already published
# (brotli, curl and OpenSSL when this was written), and a frozen tag such as
# 1.27-alpine never picks them up at all. `apk upgrade` applies those fixes,
# and CI scans the result like the application image.
#
# PACKAGE_REFRESH is only a cache key, as APT_REFRESH is in the root
# Dockerfile: CI and the core deploy pass the date, so the upgrade re-runs
# once a day instead of being served from a cached layer.
FROM nginx:1.31-alpine

ARG PACKAGE_REFRESH=""
RUN echo "package refresh: ${PACKAGE_REFRESH:-not set}" \
    && apk upgrade --no-cache
