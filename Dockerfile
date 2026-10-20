# SPDX-License-Identifier: Apache-2.0
#
# senseai-research on Railway (or any container host). Build context: the repository root.
#
# The image holds no private code. senseai-profiles is private, so it is not installed at build
# time: scripts/container-start.sh clones the profiles repository into the data volume with
# GITHUB_TOKEN at start, installs it, imports the library into the ledger, then serves.
# See docs/DEPLOY.md.

ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# deps.py reads ../pyproject.toml, so keep the package layout.
COPY packages/senseai-research/pyproject.toml /tmp/build/pyproject.toml
COPY packages/senseai-research/scripts/deps.py /tmp/build/scripts/deps.py
# Every dependency except the private senseai-profiles (installed at start), plus boto3 for
# S3 backups. Cached as its own layer. The assignment fails the build if deps.py fails.
RUN deps="$(python /tmp/build/scripts/deps.py --without senseai-profiles --extra aws)" \
    && test -n "$deps" \
    && pip install --no-cache-dir $deps

COPY packages/senseai-research /app/packages/senseai-research
COPY scripts/container-start.sh /app/scripts/container-start.sh
COPY docs /app/docs
# Runs as root: Railway mounts volumes owned by root, and the start script installs
# senseai-profiles into site-packages. The server itself only needs /data.
RUN pip install --no-cache-dir --no-deps -e /app/packages/senseai-research \
    && chmod +x /app/scripts/container-start.sh \
    && mkdir -p /data

ENV RESEARCH_DATA=/data \
    PYTHONUNBUFFERED=1

EXPOSE 8100
CMD ["/app/scripts/container-start.sh"]
