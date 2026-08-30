#!/usr/bin/env bash

set -Eeuo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "Usage: deploy_production.sh <app-root> <release-id> <image-ref> [standalone|coexist]" >&2
  exit 2
fi

APP_ROOT="$1"
RELEASE_ID="$2"
IMAGE_REF="$3"
DEPLOY_TOPOLOGY="${4:-standalone}"

[[ "$APP_ROOT" =~ ^/[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)+$ ]]
[[ "$RELEASE_ID" =~ ^sha-[0-9a-f]{12}$ ]]
[[ "$IMAGE_REF" =~ ^ghcr\.io/[a-z0-9._/-]+:sha-[0-9a-f]{12}$ ]]
case "$DEPLOY_TOPOLOGY" in
  standalone | coexist) ;;
  *)
    echo "Unsupported deployment topology: ${DEPLOY_TOPOLOGY}" >&2
    exit 2
    ;;
esac

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
if [[ "$DEPLOY_TOPOLOGY" == "coexist" ]]; then
  [[ -f "${RELEASE_DIR}/compose.coexist.yaml" ]]
  [[ -f "${RELEASE_DIR}/deploy/nginx/coexist-ip-https.conf.template" ]]
  [[ -x "${RELEASE_DIR}/scripts/reload_coexist_https.sh" ]]
fi
[[ -f "${RELEASE_DIR}/deploy/Caddyfile" ]]
[[ -f "${RELEASE_DIR}/deploy/prometheus/prometheus.yml" ]]
[[ -f "${RELEASE_DIR}/deploy/prometheus/alerts.yml" ]]
[[ -f "${RELEASE_DIR}/deploy/alertmanager/alertmanager.example.yml" ]]
[[ -f "${RELEASE_DIR}/deploy/alloy/config.alloy" ]]
[[ -f "${RELEASE_DIR}/deploy/loki/loki.yml" ]]
[[ -f "${RELEASE_DIR}/deploy/tempo/tempo.yml" ]]
[[ -f "${RELEASE_DIR}/deploy/grafana/provisioning/datasources/datasources.yml" ]]

mkdir -p "$STATE_DIR" "$BACKUP_DIR"
chmod 600 "$SHARED_ENV"
ln -sfnT "$SHARED_ENV" "${RELEASE_DIR}/.env"

ACTIVE_RELEASE_DIR="$RELEASE_DIR"
ACTIVE_IMAGE="$IMAGE_REF"
ACTIVE_TOPOLOGY="$DEPLOY_TOPOLOGY"
PREVIOUS_RELEASE=""
PREVIOUS_IMAGE=""
PREVIOUS_TOPOLOGY="standalone"
DEPLOYMENT_STARTED=0

compose_active() {
  local -a compose_arguments=(
    --env-file "$SHARED_ENV"
    -f "${ACTIVE_RELEASE_DIR}/compose.yaml"
    -f "${ACTIVE_RELEASE_DIR}/compose.prod.yaml"
  )
  if [[ "$ACTIVE_TOPOLOGY" == "coexist" ]]; then
    compose_arguments+=(
      -f "${ACTIVE_RELEASE_DIR}/compose.coexist.yaml"
    )
  fi
  COMPOSE_PROFILES="" JOB_AGENT_IMAGE="$ACTIVE_IMAGE" docker compose \
    "${compose_arguments[@]}" \
    "$@"
}

topology_files_exist() {
  local release_dir="$1"
  local topology="$2"

  [[ -f "${release_dir}/compose.yaml" && -f "${release_dir}/compose.prod.yaml" ]] || return 1
  if [[ "$topology" == "coexist" ]]; then
    [[ -f "${release_dir}/compose.coexist.yaml" ]] || return 1
  fi
}

coexist_https_available() {
  [[ "$ACTIVE_TOPOLOGY" == "coexist" ]] || return 1
  [[ -f "${ACTIVE_RELEASE_DIR}/deploy/nginx/coexist-ip-https.conf.template" ]]
}

if [[ -L "$CURRENT_LINK" ]]; then
  PREVIOUS_RELEASE="$(readlink -f "$CURRENT_LINK")"
fi
if [[ -f "${STATE_DIR}/current-image" ]]; then
  PREVIOUS_IMAGE="$(<"${STATE_DIR}/current-image")"
fi
if [[ -f "${STATE_DIR}/current-topology" ]]; then
  PREVIOUS_TOPOLOGY="$(<"${STATE_DIR}/current-topology")"
  if [[ "$PREVIOUS_TOPOLOGY" != "standalone" && "$PREVIOUS_TOPOLOGY" != "coexist" ]]; then
    echo "Invalid stored deployment topology: ${PREVIOUS_TOPOLOGY}" >&2
    exit 1
  fi
fi
if [[ "$DEPLOY_TOPOLOGY" == "coexist" || "$PREVIOUS_TOPOLOGY" == "coexist" ]]; then
  command -v curl >/dev/null 2>&1 || {
    echo "Required command is unavailable for coexist topology: curl" >&2
    exit 1
  }
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

wait_for_completed_service() {
  local service="$1"
  local timeout_seconds="${2:-120}"
  local deadline=$((SECONDS + timeout_seconds))
  local ids status exit_code

  while (( SECONDS < deadline )); do
    ids="$(compose_active ps -a -q "$service")"
    if [[ -n "$ids" ]]; then
      while IFS= read -r container_id; do
        [[ -n "$container_id" ]] || continue
        status="$(docker inspect --format '{{.State.Status}}' "$container_id")"
        if [[ "$status" == "exited" ]]; then
          exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$container_id")"
          if [[ "$exit_code" == "0" ]]; then
            return 0
          fi
          echo "Service ${service} failed with exit code ${exit_code}." >&2
          return 1
        fi
        if [[ "$status" == "dead" ]]; then
          echo "Service ${service} entered terminal state: ${status}" >&2
          return 1
        fi
      done <<< "$ids"
    fi
    sleep 2
  done

  echo "Timed out waiting for completed service: ${service}" >&2
  return 1
}

verify_coexist_web_binding() {
  local binding

  binding="$(compose_active port web 8000)"
  if [[ "$binding" != "127.0.0.1:18081" ]]; then
    echo "Coexist Web must bind to 127.0.0.1:18081; received: ${binding}" >&2
    return 1
  fi
  curl --fail --silent --show-error --max-time 10 \
    "http://${binding}/api/health" \
    >/dev/null
}

verify_coexist_https_endpoint() {
  local binding expected_public_base_url public_base_url public_ip

  binding="$(compose_active port coexist-https 8443)"
  if [[ "$binding" != "0.0.0.0:8443" ]]; then
    echo "Coexist HTTPS must bind to 0.0.0.0:8443; received: ${binding}" >&2
    return 1
  fi
  public_ip="$(
    compose_active exec -T coexist-https sh -ec \
      'printf "%s" "$JOB_AGENT_PUBLIC_IP"'
  )"
  public_base_url="$(
    compose_active exec -T coexist-https sh -ec \
      'printf "%s" "$JOB_AGENT_PUBLIC_BASE_URL"'
  )"
  if [[ ! "$public_ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    echo "Coexist HTTPS public IP is invalid: ${public_ip}" >&2
    return 1
  fi
  expected_public_base_url="https://${public_ip}:8443"
  if [[ "$public_base_url" != "$expected_public_base_url" ]]; then
    echo "JOB_AGENT_PUBLIC_BASE_URL must be ${expected_public_base_url}; received: ${public_base_url}" >&2
    return 1
  fi
  curl --fail --silent --show-error --max-time 15 \
    --resolve "${public_ip}:8443:127.0.0.1" \
    "https://${public_ip}:8443/api/health" \
    >/dev/null
}

remove_inactive_coexist_services() {
  local -a inactive_services=(reverse-proxy loki tempo alloy grafana)

  if ! compose_active stop "${inactive_services[@]}" >/dev/null; then
    echo "Failed to stop services that are inactive in coexist topology." >&2
    return 1
  fi
  if ! compose_active rm --force --stop "${inactive_services[@]}" >/dev/null; then
    echo "Failed to remove services that are inactive in coexist topology." >&2
    return 1
  fi
}

wait_for_active_topology_services() {
  local service

  if ! wait_for_completed_service alertmanager-config 120; then
    return 1
  fi
  if ! wait_for_healthy_service web 300; then
    return 1
  fi
  if [[ "$ACTIVE_TOPOLOGY" == "coexist" ]]; then
    if ! verify_coexist_web_binding; then
      return 1
    fi
    if coexist_https_available; then
      if ! wait_for_healthy_service coexist-https 180; then
        return 1
      fi
      if ! verify_coexist_https_endpoint; then
        return 1
      fi
    fi
  fi

  for service in worker beat prometheus alertmanager; do
    if ! wait_for_running_service "$service" 180; then
      return 1
    fi
  done

  if [[ "$ACTIVE_TOPOLOGY" == "standalone" ]]; then
    for service in reverse-proxy loki tempo alloy grafana; do
      if ! wait_for_running_service "$service" 180; then
        return 1
      fi
    done
  fi
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
      ]] && topology_files_exist "$PREVIOUS_RELEASE" "$PREVIOUS_TOPOLOGY"; then
    echo "Restoring previous release ${PREVIOUS_RELEASE} with image ${PREVIOUS_IMAGE}" >&2
    ACTIVE_RELEASE_DIR="$PREVIOUS_RELEASE"
    ACTIVE_IMAGE="$PREVIOUS_IMAGE"
    ACTIVE_TOPOLOGY="$PREVIOUS_TOPOLOGY"
    if ! ln -sfnT "$SHARED_ENV" "${ACTIVE_RELEASE_DIR}/.env"; then
      echo "Rollback could not restore the production environment link." >&2
      return 1
    fi
    if ! compose_active config --quiet; then
      echo "Rollback Compose configuration validation failed." >&2
      return 1
    fi
    if [[ "$ACTIVE_TOPOLOGY" == "coexist" ]]; then
      if ! remove_inactive_coexist_services; then
        return 1
      fi
    fi
    if ! compose_active up -d --no-build --pull missing --remove-orphans; then
      echo "Rollback could not start the previous release." >&2
      return 1
    fi
    if ! wait_for_active_topology_services; then
      echo "Rollback services did not become ready." >&2
      return 1
    fi
    if ! ln -sfnT "$PREVIOUS_RELEASE" "$CURRENT_LINK"; then
      echo "Rollback could not restore the current release link." >&2
      return 1
    fi
    echo "Previous application release restored. Database migrations were not reversed." >&2
    return 0
  fi

  echo "No previous release is available; stopping partially started application services." >&2
  compose_active stop \
    web worker beat reverse-proxy prometheus alertmanager loki tempo alloy grafana \
    >/dev/null 2>&1 || true
  return 1
}

on_deployment_error() {
  local exit_code=$?
  trap - ERR
  set +e
  echo "Production deployment failed for ${RELEASE_ID}." >&2
  compose_active ps >&2 || true
  if (( DEPLOYMENT_STARTED == 1 )); then
    if ! rollback_previous_release; then
      echo "Rollback failed; previous release was not restored." >&2
    fi
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
if [[ "$ACTIVE_TOPOLOGY" == "coexist" ]]; then
  remove_inactive_coexist_services
fi
compose_active up -d --no-build --pull missing --remove-orphans
wait_for_active_topology_services

ln -sfnT "$RELEASE_DIR" "$CURRENT_LINK"
printf '%s\n' "$IMAGE_REF" > "${STATE_DIR}/current-image.tmp"
mv "${STATE_DIR}/current-image.tmp" "${STATE_DIR}/current-image"
printf '%s\n' "$RELEASE_ID" > "${STATE_DIR}/current-release.tmp"
mv "${STATE_DIR}/current-release.tmp" "${STATE_DIR}/current-release"
printf '%s\n' "$DEPLOY_TOPOLOGY" > "${STATE_DIR}/current-topology.tmp"
mv "${STATE_DIR}/current-topology.tmp" "${STATE_DIR}/current-topology"
date -u +%Y-%m-%dT%H:%M:%SZ > "${STATE_DIR}/last-deployed-at.tmp"
mv "${STATE_DIR}/last-deployed-at.tmp" "${STATE_DIR}/last-deployed-at"
chmod 600 \
  "${STATE_DIR}/current-image" \
  "${STATE_DIR}/current-release" \
  "${STATE_DIR}/current-topology" \
  "${STATE_DIR}/last-deployed-at"

DEPLOYMENT_STARTED=0
trap - ERR
compose_active ps
echo "Production deployment completed: ${RELEASE_ID}"
