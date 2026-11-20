# syntax=docker/dockerfile:1
# SetuHaul app image (linux/arm64 for t4g; builds on amd64 too). Build from the REPO ROOT:
#   docker build -f deploy/server/Dockerfile -t setuhaul-app:<tag> .
# Only prototype/ and the entrypoint are sent to the build (see Dockerfile.dockerignore).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app
WORKDIR /app

# Wheels only: every pin has a manylinux aarch64 / py3 wheel (checked 2026-11-20), so nothing compiles on the 1 GiB box.
COPY prototype/requirements-prod.txt .
RUN pip install --only-binary=:all: -r requirements-prod.txt

# app.py lives at prototype/app.py and is run as "app:app" from that folder (it imports setuhaul.*).
COPY --chown=app:app prototype/ /app/
COPY --chown=app:app --chmod=755 deploy/server/entrypoint.sh /usr/local/bin/setuhaul-entrypoint
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["/usr/local/bin/setuhaul-entrypoint"]
