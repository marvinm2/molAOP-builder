# --- Build stage ---
FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential
COPY requirements.txt .
# torch's +cpu wheel index URL is needed at install time, but the version
# itself is pinned in requirements.txt — listing it here too would conflict
# with bumps (see CI failure on commit 1c51bec). Single source of truth.
RUN pip install --no-cache-dir --prefix=/install \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    -r requirements.txt

# --- Runtime stage ---
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FLASK_APP=app.py FLASK_ENV=production
WORKDIR /app
# sqlite3 is the backup script's engine (Online Backup API). No cron package:
# the container runs a single gunicorn process as a non-root user, so it cannot
# start a cron daemon — backups are scheduled on the Swarm instead (issue #215,
# see "Database backups" in CLAUDE.md).
RUN apt-get update && apt-get install -y --no-install-recommends curl sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /var/cache/debconf/*-old /var/lib/dpkg/*-old /var/log/dpkg.log /var/log/apt/*

# #158 follow-up: align container uid/gid with the host owner of the bind-
# mounted /app/data volume (currently /mnt/gluster/docker/molaop-builder/data
# on tgx1, owned by mmartens uid=1000). Pass APP_UID/APP_GID at build time
# if the host owner differs. Default 1000 covers the typical Linux uid and
# matches the current production deployment.
#
# Note this does NOT govern writes to pre-existing files on the bind mount:
# those fail when the file's group differs from the container's gid, which no
# build arg can change. See docs/RELEASES.md § Known limitations.
ARG APP_UID=1000
ARG APP_GID=1000
RUN addgroup --gid ${APP_GID} appuser \
    && adduser --uid ${APP_UID} --gid ${APP_GID} --disabled-password --gecos '' appuser

COPY --from=builder /install /usr/local
# --chown at copy time rather than a following `chown -R /app`: chown -R rewrites
# every file's metadata, which makes Docker write a second full copy of /app into
# a new layer. That duplication was ~7MB of the image and showed up as every
# application file appearing twice in `dive`.
COPY --chown=appuser:appuser . .
RUN mkdir -p /app/static/css /app/static/js /app/data /app/logs /app/data/backups \
    && chown appuser:appuser /app/static/css /app/static/js /app/data /app/logs /app/data/backups
# Make scripts executable
RUN chmod +x /app/scripts/backup_db.sh /app/scripts/docker-entrypoint.sh
USER appuser
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=30s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
