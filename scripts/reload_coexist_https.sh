#!/usr/bin/env bash

set -Eeuo pipefail

if [[ $# -gt 1 ]]; then
  echo "Usage: reload_coexist_https.sh [app-root]" >&2
  exit 2
fi

APP_ROOT="${1:-/opt/job-hunting-agent}"
[[ "$APP_ROOT" =~ ^/[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)+$ ]]

CURRENT_DIR="${APP_ROOT}/current"
STATE_DIR="${APP_ROOT}/state"
SHARED_ENV="${APP_ROOT}/shared/.env"

if [[ ! -d "$CURRENT_DIR" || ! -f "$SHARED_ENV" || ! -f "${STATE_DIR}/current-image" ]]; then
  exit 0
fi
if [[ ! -f "${CURRENT_DIR}/compose.coexist.yaml" ]]; then
  exit 0
fi

ACTIVE_IMAGE="$(<"${STATE_DIR}/current-image")"
[[ "$ACTIVE_IMAGE" =~ ^ghcr\.io/[a-z0-9._/-]+:sha-[0-9a-f]{12}$ ]]

compose_current() {
  COMPOSE_PROFILES="" JOB_AGENT_IMAGE="$ACTIVE_IMAGE" docker compose \
    --env-file "$SHARED_ENV" \
    -f "${CURRENT_DIR}/compose.yaml" \
    -f "${CURRENT_DIR}/compose.prod.yaml" \
    -f "${CURRENT_DIR}/compose.coexist.yaml" \
    "$@"
}

container_id="$(compose_current ps -q coexist-https)"
if [[ -z "$container_id" ]]; then
  exit 0
fi
if [[ "$(docker inspect --format '{{.State.Running}}' "$container_id")" != "true" ]]; then
  echo "The coexist HTTPS edge is not running." >&2
  exit 1
fi

compose_current exec -T coexist-https nginx -t
compose_current exec -T coexist-https nginx -s reload
