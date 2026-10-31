#
# NAS Transcode Studio — deliberately small and compile-free.
# All media work is delegated to Alpine packages; the application itself is
# pure Python standard library.  A cold build is one `apk add` layer.
#
ARG BASE_IMAGE=alpine:3.20
FROM ${BASE_IMAGE}

ARG APK_MIRROR=https://mirrors.aliyun.com/alpine
LABEL org.opencontainers.image.title="NAS Transcode Studio"
LABEL org.opencontainers.image.description="Read-only NAS media transcoder with persistent queue and modern WebUI"
LABEL org.opencontainers.image.version="1.0.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    MAGICK_TMPDIR=/tmp

# APK mirror is supplied by compose by default for fast mainland-China builds.
# It only rewrites the package repository URLs; no extra frontend image is pulled.
# Override with: docker compose build --build-arg APK_MIRROR=...
RUN if [ -n "$APK_MIRROR" ]; then \
        sed -i "s|https://dl-cdn.alpinelinux.org/alpine|$APK_MIRROR|g" /etc/apk/repositories; \
    fi \
    && apk add --no-cache \
        python3 \
        ffmpeg \
        imagemagick \
        7zip \
        libarchive-tools \
        ca-certificates \
        tzdata \
    && rm -rf /var/cache/apk/* /tmp/*

WORKDIR /app
COPY app /app/app
RUN mkdir -p /data /output /tmp/.cache \
    && chmod 1777 /tmp

EXPOSE 5124
VOLUME ["/data", "/output"]
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=6s --start-period=25s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5124/api/health', timeout=4)"

CMD ["python3", "-m", "app.server"]
