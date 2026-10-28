# Certificate renewal. The upstream image carries fixable HIGH and CRITICAL
# findings in Alpine packages and in Python packages it only needed to build
# itself, so this one upgrades what certbot uses and removes what it doesn't.
#
# pip goes too: its vendored urllib3, msgpack and certifi are older copies that
# the scanner reports on their own, and renewing a certificate never installs
# anything. /opt/certbot/tools is the upstream build's requirements file.
#
# The final stage copies the result into one layer: a file deleted in a later
# layer is still in the one below it, and the scanner reads it there.
FROM certbot/certbot:v5.8.0 AS upstream

ARG PACKAGE_REFRESH=""
RUN echo "package refresh: ${PACKAGE_REFRESH:-not set}" \
    && apk upgrade --no-cache \
    && pip install --no-cache-dir --upgrade \
        certifi "urllib3>=2.8.0" "msgpack>=1.2.3" "setuptools>=78.1.1" \
    && pip uninstall -y uv \
    && python -m pip uninstall -y pip \
    && rm -rf /opt/certbot/tools /usr/local/lib/python3.14/ensurepip/_bundled \
    && certbot --version

FROM scratch
COPY --from=upstream / /
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8
WORKDIR /opt/certbot
VOLUME ["/etc/letsencrypt", "/var/lib/letsencrypt"]
ENTRYPOINT ["certbot"]
