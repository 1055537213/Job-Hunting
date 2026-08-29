#!/usr/bin/env bash

set -Eeuo pipefail

if [[ $# -ne 3 ]]; then
  echo "Usage: deploy_production.sh <app-root> <release-id> <image-ref>" >&2
  exit 2
fi

APP_ROOT="$1"
RELEASE_ID="$2"
IMAGE_REF="$3"

[[ "$APP_ROOT" =~ ^/[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)+$ ]]
[[ "$RELEASE_ID" =~ ^sha-[0-9a-f]{12}$ ]]
[[ "$IMAGE_REF" =~ ^ghcr\.io/[a-z0-9._/-]+:sha-[0-9a-f]{12}$ ]]

RELEASE_DIR="${APP_ROOT}/releases/${RELEASE_ID}"
SHARED_ENV="${APP_ROOT}/shared/.env"
CURRENT_LINK="${APP_ROOT}/current"
STATE_DIR="${APP_ROOT}/state"
BACKUP_DIR="${APP_ROOT}/backups"

for required_command in docker readlink sha256sum; do
  command -v "$required_command" >/dev/null 2>&1 || {
    echo "Required command is unavailable: ${required_command}" >&2
    exit 1
  }
done
docker compose version >/dev/null

[[ -f "$SHARED_ENV" ]] || {
  echo "Missing production environment file: ${SHARED_ENV}" >&2
  exit 1
}
[[ -f "${RELEASE_DIR}/compose.yaml" ]]
[[ -f "${RELEASE_DIR}/compose.prod.yaml" ]]
[[ -f "${RELEASE_DIR}/deploy/Caddyfile" ]]
[[ -f "${RELEASE_DIR}/deploy/prometheus/prometheus.yml" ]]
[[ -f "${RELEASE_DIR}/deploy/prometheus/alerts.yml" ]]

mkdir -p "$STATE_DIR" "$BACKUP_DIR"
chmod 600 "$SHARED_ENV"
ln -sfnT "$SHARED_ENV" "${RELEASE_DIR}/.env"

ACTIVE_RELEASE_DIR="$RELEASE_DIR"
ACTIVE_IMAGE="$IMAGE_REF"
PREVIOUS_RELEASE=""
PREVIOUS_IMAGE=""
DEPLOYMENT_STARTED=0

compose_active() {
  JOB_AGENT_IMAGE="$ACTIVE_IMAGE" docker compose \
    --env-file "$SHARED_ENV" \
    -f "${ACTIVE_RELEASE_DIR}/compose.yaml" \
    -f "${ACTIVE_RELEASE_DIR}/compose.prod.yaml" \
    "$@"
}

if [[ -L "$CURRENT_LINK" ]]; then
  PREVIOUS_RELEASE="$(readlink -f "$CURRENT_LINK")"
fi
if [[ -f "${STATE_DIR}/current-image" ]]; then
  PREVIOUS_IMAGE="$(<"${STATE_DIR}/current-image")"
fi

wait_for_healthy_service() {
  local service="$1"
  local timeout_seconds="${2:-300}"
  local deadline=$((SECONDS + timeout_seconds))
  local ids status all_healthy

  while (( SECONDS < deadline )); do
    ids="$(compose_active ps -q "$service")"
    if [[ -n "$ids" ]]; then
      all_healthy=1
      while IFS= read -r container_id; do
        [[ -n "$container_id" ]] || continue
        status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
        if [[ "$status" == "unhealthy" || "$status" == "exited" || "$status" == "dead" ]]; then
          echo "Service ${service} entered terminal state: ${status}" >&2
          return 1
        fi
        if [[ "$status" != "healthy" ]]; then
          all_healthy=0
        fi
      done <<< "$ids"
      if (( all_healthy == 1 )); then
        return 0
      fi
    fi
    sleep 5
  done

  echo "Timed out waiting for healthy service: ${service}" >&2
  return 1
}

wait_for_running_service() {
  local service="$1"
  local timeout_seconds="${2:-180}"
  local deadline=$((SECONDS + timeout_seconds))
  local ids status all_running

  while (( SECONDS < deadline )); do
    ids="$(compose_active ps -q "$service")"
    if [[ -n "$ids" ]]; then
      all_running=1
      while IFS= read -r container_id; do
        [[ -n "$container_id" ]] || continue
        status="$(docker inspect --format '{{.State.Status}}' "$container_id")"
        if [[ "$status" == "exited" || "$status" == "dead" ]]; then
          echo "Service ${service} entered terminal state: ${status}" >&2
          return 1
        fi
        if [[ "$status" != "running" ]]; then
          all_running=0
        fi
      done <<< "$ids"
      if (( all_running == 1 )); then
        return 0
      fi
    fi
    sleep 5
  done

  echo "Timed out waiting for running service: ${service}" >&2
  return 1
}

backup_database_if_running() {
  local postgres_id backup_file temporary_file
  postgres_id="$(compose_active ps -q postgres || true)"
  [[ -n "$postgres_id" ]] || return 0
  [[ "$(docker inspect --format '{{.State.Running}}' "$postgres_id")" == "true" ]] || return 0

  backup_file="${BACKUP_DIR}/predeploy-${RELEASE_ID}-$(date -u +%Y%m%dT%H%M%SZ).dump"
  temporary_file="${backup_file}.tmp"
  compose_active exec -T postgres sh -ec \
    'pg_dump --format=custom --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"' \
    > "$temporary_file"
  [[ -s "$temporary_file" ]]
  mv "$temporary_file" "$backup_file"
  sha256sum "$backup_file" > "${backup_file}.sha256"
  chmod 600 "$backup_file" "${backup_file}.sha256"
  echo "Created pre-deployment database backup: ${backup_file}"
}

rollback_previous_release() {
  if [[ -n "$PREVIOUS_RELEASE" && -n "$PREVIOUS_IMAGE" \
      && -f "${PREVIOUS_RELEASE}/compose.yaml" \
      && -f "${PREVIOUS_RELEASE}/compose.prod.yaml" ]]; then
    echo "Restoring previous release ${PREVIOUS_RELEASE} with image ${PREVIOUS_IMAGE}" >&2
    ACTIVE_RELEASE_DIR="$PREVIOUS_RELEASE"
    ACTIVE_IMAGE="$PREVIOUS_IMAGE"
    ln -sfnT "$SHARED_ENV" "${ACTIVE_RELEASE_DIR}/.env"
    compose_active config --quiet
    compose_active up -d --no-build --pull missing --remove-orphans
    wait_for_healthy_service web 300
    ln -sfnT "$PREVIOUS_RELEASE" "$CURRENT_LINK"
    echo "Previous application release restored. Database migrations were not reversed." >&2
    return 0
  fi

  echo "No previous release is available; stopping partially started application services." >&2
  compose_active stop web worker beat reverse-proxy prometheus >/dev/null 2>&1 || true
  return 1
}

on_deployment_error() {
  local exit_code=$?
  trap - ERR
  set +e
  echo "Production deployment failed for ${RELEASE_ID}." >&2
  compose_active ps >&2 || true
  if (( DEPLOYMENT_STARTED == 1 )); then
    rollback_previous_release || true
  fi
  exit "$exit_code"
}

trap on_deployment_error ERR

docker image inspect "$IMAGE_REF" >/dev/null
IMAGE_REVISION="$(docker image inspect "$IMAGE_REF" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
[[ "$IMAGE_REVISION" =~ ^[0-9a-f]{40}$ ]]
[[ "sha-${IMAGE_REVISION:0:12}" == "$RELEASE_ID" ]]
compose_active config --quiet
backup_database_if_running

DEPLOYMENT_STARTED=1
compose_active up -d --no-build --pull missing --remove-orphans
wait_for_healthy_service web 300
wait_for_running_service worker 180
wait_for_running_service beat 180
wait_for_running_service reverse-proxy 180
wait_for_running_service prometheus 180

ln -sfnT "$RELEASE_DIR" "$CURRENT_LINK"
printf '%s\n' "$IMAGE_REF" > "${STATE_DIR}/current-image.tmp"
mv "${STATE_DIR}/current-image.tmp" "${STATE_DIR}/current-image"
printf '%s\n' "$RELEASE_ID" > "${STATE_DIR}/current-release.tmp"
mv "${STATE_DIR}/current-release.tmp" "${STATE_DIR}/current-release"
date -u +%Y-%m-%dT%H:%M:%SZ > "${STATE_DIR}/last-deployed-at.tmp"
mv "${STATE_DIR}/last-deployed-at.tmp" "${STATE_DIR}/last-deployed-at"
chmod 600 "${STATE_DIR}/current-image" "${STATE_DIR}/current-release" "${STATE_DIR}/last-deployed-at"

DEPLOYMENT_STARTED=0
trap - ERR
compose_active ps
echo "Production deployment completed: ${RELEASE_ID}"
