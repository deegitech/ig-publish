# syntax=docker/dockerfile:1
# ig-publish: a small, non-root image with ffmpeg (for `prep`).
#
#   docker build -f docker/Dockerfile -t ig-publish:0.1.0 .
#   docker run --rm -v "$PWD/data:/data" ig-publish:0.1.0 plan --offline
#   docker run --rm -v "$PWD/data:/data" ig-publish:0.1.0                         # = schedule dry (never publishes)
#   docker run -d ... ig-publish:0.1.0 schedule run --apply                        # the scheduler (docs/deploy-ec2.md)
#
# /data holds ig-publish.toml or ig-publish.json, the manifest, the schedule, media/ and state/.
# The base image is pinned by digest (python:3.12-slim); Dependabot proposes updates.
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

COPY pyproject.toml README.md LICENSE /tmp/ig-publish/
COPY src /tmp/ig-publish/src
RUN pip install /tmp/ig-publish && rm -rf /tmp/ig-publish

RUN groupadd --system --gid 10001 igpublish \
 && useradd --system --uid 10001 --gid 10001 --home-dir /data --no-create-home \
        --shell /usr/sbin/nologin igpublish \
 && mkdir -p /data \
 && chown 10001:10001 /data

WORKDIR /data
USER 10001:10001
VOLUME ["/data"]

# Without --config, ig-publish uses $IG_PUBLISH_CONFIG or ./ig-publish.toml / ./ig-publish.json in /data.
# The default command only previews; publishing needs an explicit `schedule run --apply` (or `publish ... --apply`).
ENTRYPOINT ["ig-publish"]
CMD ["schedule", "dry"]
