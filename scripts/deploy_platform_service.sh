#!/usr/bin/env bash

set -Eeuo pipefail

if [[ $# -ne 3 ]]; then
  echo "Usage: deploy_platform_service.sh <app-root> <release-id> <image-ref>" >&2
  exit 2
fi

APP_ROOT="$1"
RELEASE_ID="$2"
IMAGE_REF="$3"
[[ "$APP_ROOT" =~ ^/[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)+$ ]]
[[ "$RELEASE_ID" =~ ^sha-[0-9a-f]{12}$ ]]
[[ "$IMAGE_REF" =~ ^ghcr\.io/[a-z0-9._/-]+-platform:sha-[0-9a-f]{12}$ ]]

RELEASE_DIR="${APP_ROOT}/platform-releases/${RELEASE_ID}"
SHARED_ENV="${APP_ROOT}/shared/.env"
STATE_DIR="${APP_ROOT}/platform-state"
CURRENT_LINK="${APP_ROOT}/platform-current"
LOCK_DIR="${STATE_DIR}/deploy.lock"
NETWORK="job-hunting-agent-production_default"
[[ -f "${RELEASE_DIR}/compose.platform.prod.yaml" ]]
[[ -f "$SHARED_ENV" ]]
chmod 600 "$SHARED_ENV"
docker compose version >/dev/null
docker network inspect "$NETWORK" >/dev/null
mkdir -p "$STATE_DIR"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "Another Java platform deployment is running." >&2
  exit 1
fi
trap 'rmdir "$LOCK_DIR" >/dev/null 2>&1 || true' EXIT

PREVIOUS_RELEASE=""
PREVIOUS_IMAGE=""
if [[ -L "$CURRENT_LINK" ]]; then
  PREVIOUS_RELEASE="$(readlink -f "$CURRENT_LINK")"
fi
if [[ -f "${STATE_DIR}/current-image" ]]; then
  PREVIOUS_IMAGE="$(<"${STATE_DIR}/current-image")"
fi

ACTIVE_RELEASE="$RELEASE_DIR"
ACTIVE_IMAGE="$IMAGE_REF"
STARTED=0

compose_platform() {
  JOB_AGENT_PLATFORM_IMAGE="$ACTIVE_IMAGE" \
    docker compose --env-file "$SHARED_ENV" \
      -f "${ACTIVE_RELEASE}/compose.platform.prod.yaml" "$@"
}

wait_healthy() {
  local deadline=$((SECONDS + 180))
  local container_id status
  while (( SECONDS < deadline )); do
    container_id="$(compose_platform ps -q platform-service)"
    if [[ -n "$container_id" ]]; then
      status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
      if [[ "$status" == "healthy" ]]; then
        return 0
      fi
      if [[ "$status" == "unhealthy" || "$status" == "exited" || "$status" == "dead" ]]; then
        echo "Java platform service entered terminal state: $status" >&2
        return 1
      fi
    fi
    sleep 5
  done
  echo "Timed out waiting for the Java platform health check." >&2
  return 1
}

on_error() {
  local exit_code=$?
  trap - ERR
  set +e
  echo "Java platform deployment failed for $RELEASE_ID." >&2
  compose_platform logs --no-color --tail 100 platform-service >&2 || true
  if (( STARTED == 1 )); then
    if [[ -n "$PREVIOUS_RELEASE" && -n "$PREVIOUS_IMAGE" \
        && -f "${PREVIOUS_RELEASE}/compose.platform.prod.yaml" ]]; then
      ACTIVE_RELEASE="$PREVIOUS_RELEASE"
      ACTIVE_IMAGE="$PREVIOUS_IMAGE"
      if compose_platform config --quiet \
          && compose_platform up -d --no-build --no-deps --pull never platform-service \
          && wait_healthy; then
        echo "Previous Java platform release restored." >&2
      else
        echo "Java platform rollback failed; inspect the container immediately." >&2
      fi
    else
      compose_platform stop platform-service >/dev/null 2>&1 || true
      echo "First-time Java platform deployment stopped; the Python application was not changed." >&2
    fi
  fi
  exit "$exit_code"
}
trap on_error ERR

docker image inspect "$IMAGE_REF" >/dev/null
IMAGE_REVISION="$(docker image inspect "$IMAGE_REF" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
[[ "$IMAGE_REVISION" =~ ^[0-9a-f]{40}$ ]]
[[ "sha-${IMAGE_REVISION:0:12}" == "$RELEASE_ID" ]]
compose_platform config --quiet

STARTED=1
compose_platform up -d --no-build --no-deps --pull never platform-service
wait_healthy

ln -sfnT "$RELEASE_DIR" "$CURRENT_LINK"
printf '%s\n' "$IMAGE_REF" > "${STATE_DIR}/current-image.tmp"
mv "${STATE_DIR}/current-image.tmp" "${STATE_DIR}/current-image"
printf '%s\n' "$RELEASE_ID" > "${STATE_DIR}/current-release.tmp"
mv "${STATE_DIR}/current-release.tmp" "${STATE_DIR}/current-release"
chmod 600 "${STATE_DIR}/current-image" "${STATE_DIR}/current-release"
STARTED=0
trap - ERR
compose_platform ps platform-service
echo "Java platform deployment completed: $RELEASE_ID (billing disabled)"
