FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations

# Pick up Debian security fixes the slim base image has not been rebuilt with yet.
#
# Then drop what FFmpeg's package chain brings but a command-line encode never
# loads: Mesa's GPU drivers (LLVM, gallium, z3 -- about 190 MB) are opened only
# by an OpenGL context, which nothing here creates; `ldd` on ffmpeg and ffprobe
# lists none of them. Every spot worker pulls this image as it boots, so its
# size is part of the first job's wait.
#
# The last two steps prove it on every build: a real encode through the HDR
# tone-map chain and libx264, and a probe. If removing those files ever breaks
# what the workers run, the build fails here instead of the first job.
# Licences (`copyright` files) are kept; the rest of /usr/share/doc goes.
#
# APT_REFRESH is only a cache key. A cached copy of this layer keeps the
# packages of the day it was first built, so a Debian fix published since
# never arrives; CI passes the date, which re-runs the upgrade once a day.
ARG APT_REFRESH=""
RUN echo "apt refresh: ${APT_REFRESH:-not set}" \
    && apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /usr/lib/*-linux-gnu/libLLVM*.so* /usr/lib/*-linux-gnu/libgallium*.so* \
        /usr/lib/*-linux-gnu/libz3.so* \
    && find /usr/share/doc -type f ! -name copyright -delete \
    && rm -rf /usr/share/man \
    && ffmpeg -hide_banner -v error -f lavfi -i testsrc2=size=64x64:rate=5:duration=1 \
        -vf "setparams=color_trc=smpte2084:color_primaries=bt2020:colorspace=bt2020nc,zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p" \
        -c:v libx264 -preset ultrafast -f null - \
    && ffprobe -hide_banner -v error -f lavfi -i testsrc2=size=64x64:rate=5:duration=1 \
        -show_format >/dev/null

RUN python -m pip install --no-cache-dir .

# Drop root for the runtime. The install above needs it to write into the
# system site-packages; nothing after it does. /app deliberately stays owned by
# root and read-only to this user -- the process reads its own code and never
# writes it, and PYTHONDONTWRITEBYTECODE stops Python trying to drop .pyc files
# beside it. Uploads spool to /tmp, which stays writable, and binding port 8000
# needs no privilege. Both the api and worker services run from this image, so
# both drop root together.
RUN useradd --system --create-home --uid 10001 flickpond
USER flickpond

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]

