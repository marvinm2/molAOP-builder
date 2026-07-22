#!/bin/bash
# /app/scripts/docker-entrypoint.sh
# Exec the main process (gunicorn). Using exec ensures gunicorn receives Docker
# stop signals correctly.
#
# This used to run `service cron start || true` to schedule backup_db.sh. That
# never worked: the line runs after `USER appuser` in the Dockerfile, so
# starting the daemon needs root and always failed — silently, because of the
# `|| true`. The result was a backup mechanism that looked deployed for months
# without ever producing a single backup (issue #215). Backups are now a Swarm
# cron job; see "Database backups" in CLAUDE.md.
set -e

# Hand off to gunicorn (or whatever CMD is passed)
exec "$@"
