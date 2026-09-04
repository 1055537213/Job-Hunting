#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

if [[ $# -ne 2 || "$2" != "BACKUP_AND_VALIDATE" ]]; then
  echo "Usage: validate_production_recovery.sh <app-root> BACKUP_AND_VALIDATE" >&2
  exit 2
fi

APP_ROOT="$1"
PROJECT_NAME="job-hunting-agent-production"
CURRENT_LINK="${APP_ROOT}/current"
SHARED_ENV="${APP_ROOT}/shared/.env"
STATE_DIR="${APP_ROOT}/state"
BACKUP_ROOT="${APP_ROOT}/backups"
LOCK_DIR="${STATE_DIR}/production-recovery.lock"

[[ "$APP_ROOT" =~ ^/[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)+$ ]] || {
  echo "Invalid application root: ${APP_ROOT}" >&2
  exit 2
}

for command_name in docker readlink sha256sum openssl python3 date; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is unavailable: ${command_name}" >&2
    exit 1
  }
done
docker compose version >/dev/null

[[ -L "$CURRENT_LINK" ]] || {
  echo "Current release link is missing: ${CURRENT_LINK}" >&2
  exit 1
}
[[ -f "$SHARED_ENV" ]] || {
  echo "Production environment file is missing: ${SHARED_ENV}" >&2
  exit 1
}
[[ -f "${STATE_DIR}/current-image" ]] || {
  echo "Current image state is missing." >&2
  exit 1
}
[[ -f "${STATE_DIR}/current-topology" ]] || {
  echo "Current topology state is missing." >&2
  exit 1
}

RELEASE_DIR="$(readlink -f "$CURRENT_LINK")"
IMAGE_REF="$(<"${STATE_DIR}/current-image")"
TOPOLOGY="$(<"${STATE_DIR}/current-topology")"
[[ "$RELEASE_DIR" == "${APP_ROOT}/releases/"* ]] || {
  echo "Current release resolves outside the release directory: ${RELEASE_DIR}" >&2
  exit 1
}
[[ "$IMAGE_REF" =~ ^ghcr\.io/[a-z0-9._/-]+:sha-[0-9a-f]{12}$ ]] || {
  echo "Current image state is invalid." >&2
  exit 1
}
[[ "$TOPOLOGY" == "standalone" || "$TOPOLOGY" == "coexist" ]] || {
  echo "Current topology state is invalid: ${TOPOLOGY}" >&2
  exit 1
}

COMPOSE_FILES=(
  -f "${RELEASE_DIR}/compose.yaml"
  -f "${RELEASE_DIR}/compose.prod.yaml"
)
if [[ "$TOPOLOGY" == "coexist" ]]; then
  COMPOSE_FILES+=( -f "${RELEASE_DIR}/compose.coexist.yaml" )
fi

compose_production() {
  COMPOSE_PROFILES="" \
    JOB_AGENT_IMAGE="$IMAGE_REF" \
    JOB_AGENT_RUNTIME_ENV_FILE="$SHARED_ENV" \
    docker compose \
    --project-name "$PROJECT_NAME" \
    --env-file "$SHARED_ENV" \
    "${COMPOSE_FILES[@]}" \
    "$@"
}

mkdir -p "$STATE_DIR" "$BACKUP_ROOT"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "Another production recovery operation is already active: ${LOCK_DIR}" >&2
  exit 1
fi

STAMP="$(date -u +%Y%m%d-%H%M%S)"
SUFFIX="$(openssl rand -hex 6)"
BACKUP_DIR="${BACKUP_ROOT}/${STAMP}-${SUFFIX}"
REPORT_PATH="${BACKUP_DIR}/restore-validation.json"
POSTGRES_DUMP="${BACKUP_DIR}/postgres.dump"
MINIO_ARCHIVE="${BACKUP_DIR}/minio-data.tar.gz"
MANIFEST_PATH="${BACKUP_DIR}/manifest.json"
RECOVERY_PREFIX="job-agent-recovery-${SUFFIX}"
RECOVERY_POSTGRES="${RECOVERY_PREFIX}-postgres"
RECOVERY_MINIO="${RECOVERY_PREFIX}-minio"
RECOVERY_NETWORK="${RECOVERY_PREFIX}-network"
RECOVERY_POSTGRES_VOLUME="${RECOVERY_PREFIX}-postgres-data"
RECOVERY_MINIO_VOLUME="${RECOVERY_PREFIX}-minio-data"
PRODUCTION_QUIESCED=0
ISOLATED_CREATED=0
FAILED_LINE=0

record_failure_line() {
  FAILED_LINE="$1"
}
trap 'record_failure_line "$LINENO"' ERR

cleanup() {
  local exit_code=$?
  local restart_failed=0
  set +e

  if (( exit_code != 0 )) && [[ -d "$BACKUP_DIR" ]]; then
    FAILED_LINE_VALUE="$FAILED_LINE" \
      ISOLATED_CREATED_VALUE="$ISOLATED_CREATED" \
      python3 - "$REPORT_PATH" <<'PY'
import json
import os
import sys

report = {
    "result": "FAILED",
    "failed_line": int(os.environ.get("FAILED_LINE_VALUE", "0")),
    "production_data_modified": False,
    "isolated_restore_used_unique_volumes": os.environ.get("ISOLATED_CREATED_VALUE") == "1",
    "note": "Inspect the command output. Production restart is attempted before the script exits.",
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(report, handle, ensure_ascii=True, indent=2)
    handle.write("\n")
PY
    chmod 600 "$REPORT_PATH" >/dev/null 2>&1 || true
  fi

  if (( ISOLATED_CREATED == 1 )); then
    [[ "$RECOVERY_PREFIX" =~ ^job-agent-recovery-[0-9a-f]{12}$ ]] || {
      echo "Refusing cleanup for unexpected recovery prefix: ${RECOVERY_PREFIX}" >&2
      exit 1
    }
    docker rm -f "$RECOVERY_POSTGRES" "$RECOVERY_MINIO" >/dev/null 2>&1 || true
    docker volume rm "$RECOVERY_POSTGRES_VOLUME" "$RECOVERY_MINIO_VOLUME" >/dev/null 2>&1 || true
    docker network rm "$RECOVERY_NETWORK" >/dev/null 2>&1 || true
  fi

  if (( PRODUCTION_QUIESCED == 1 )); then
    echo "==> Restoring production services"
    if ! compose_production up -d --no-build; then
      echo "CRITICAL: automatic production service restart failed." >&2
      restart_failed=1
    fi
  fi

  rmdir "$LOCK_DIR" >/dev/null 2>&1 || true
  if (( restart_failed == 1 )); then
    exit 1
  fi
  exit "$exit_code"
}
trap cleanup EXIT

wait_for_container() {
  local container_name="$1"
  local check_command="$2"
  local deadline=$((SECONDS + 180))

  while (( SECONDS < deadline )); do
    if docker exec "$container_name" sh -ec "$check_command" >/dev/null 2>&1; then
      return 0
    fi
    if [[ "$(docker inspect --format '{{.State.Status}}' "$container_name" 2>/dev/null || true)" =~ ^(exited|dead)$ ]]; then
      docker logs "$container_name" >&2 || true
      return 1
    fi
    sleep 2
  done
  echo "Timed out waiting for container: ${container_name}" >&2
  return 1
}

wait_for_production_service() {
  local service="$1"
  local deadline=$((SECONDS + 300))
  local ids state health all_ready

  while (( SECONDS < deadline )); do
    ids="$(compose_production ps -q "$service" 2>/dev/null || true)"
    if [[ -n "$ids" ]]; then
      all_ready=1
      while IFS= read -r container_id; do
        [[ -n "$container_id" ]] || continue
        state="$(docker inspect --format '{{.State.Status}}' "$container_id" 2>/dev/null || true)"
        health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
        if [[ "$state" != "running" || ( -n "$health" && "$health" != "healthy" ) ]]; then
          all_ready=0
        fi
      done <<< "$ids"
      if (( all_ready == 1 )); then
        return 0
      fi
    fi
    sleep 3
  done
  echo "Timed out waiting for production service: ${service}" >&2
  return 1
}

echo "==> Validating the active production topology"
compose_production config --quiet
POSTGRES_ID="$(compose_production ps -q postgres)"
MINIO_ID="$(compose_production ps -q minio)"
WEB_ID="$(compose_production ps -q web | head -n 1)"
[[ -n "$POSTGRES_ID" && -n "$MINIO_ID" && -n "$WEB_ID" ]] || {
  echo "Production PostgreSQL, MinIO, or Web container is not running." >&2
  exit 1
}

POSTGRES_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$POSTGRES_ID")"
MINIO_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$MINIO_ID")"
MINIO_VOLUME="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$MINIO_ID")"
POSTGRES_USER="$(docker exec "$POSTGRES_ID" printenv POSTGRES_USER)"
POSTGRES_DB="$(docker exec "$POSTGRES_ID" printenv POSTGRES_DB)"
OBJECT_BUCKET="$(docker exec "$WEB_ID" printenv JOB_AGENT_OBJECT_STORAGE_BUCKET)"

[[ -n "$POSTGRES_IMAGE" && -n "$MINIO_IMAGE" && -n "$MINIO_VOLUME" ]] || {
  echo "Could not resolve production images or the MinIO volume." >&2
  exit 1
}
[[ "$POSTGRES_USER" =~ ^[A-Za-z_][A-Za-z0-9_]*$ && "$POSTGRES_DB" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || {
  echo "PostgreSQL identity contains unsupported characters." >&2
  exit 1
}
[[ "$OBJECT_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || {
  echo "Object-storage bucket name is invalid." >&2
  exit 1
}

DATABASE_BYTES="$(docker exec "$POSTGRES_ID" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SELECT pg_database_size(current_database())')"
MINIO_BYTES="$(docker run --rm --entrypoint sh -v "${MINIO_VOLUME}:/data:ro" "$POSTGRES_IMAGE" -ec "du -sb /data | cut -f1")"
AVAILABLE_KB="$(df -Pk "$APP_ROOT" | awk 'NR == 2 {print $4}')"
[[ "$DATABASE_BYTES" =~ ^[0-9]+$ && "$MINIO_BYTES" =~ ^[0-9]+$ && "$AVAILABLE_KB" =~ ^[0-9]+$ ]] || {
  echo "Could not determine numeric storage requirements." >&2
  exit 1
}
REQUIRED_BYTES=$((DATABASE_BYTES * 2 + MINIO_BYTES * 3 + 536870912))
AVAILABLE_BYTES=$((AVAILABLE_KB * 1024))
if (( AVAILABLE_BYTES < REQUIRED_BYTES )); then
  echo "Insufficient disk space for backup and isolated restore. Required=${REQUIRED_BYTES}, available=${AVAILABLE_BYTES}." >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
STARTED_EPOCH="$(date +%s)"

echo "==> Entering a brief production maintenance window"
PRODUCTION_QUIESCED=1
if [[ "$TOPOLOGY" == "coexist" ]]; then
  compose_production stop coexist-https
else
  compose_production stop reverse-proxy
fi
compose_production stop web worker beat

REMOTE_DUMP="/tmp/job-agent-${STAMP}-${SUFFIX}.dump"
docker exec "$POSTGRES_ID" pg_dump \
  --format=custom --no-owner --no-acl \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$REMOTE_DUMP"
docker cp "${POSTGRES_ID}:${REMOTE_DUMP}" "$POSTGRES_DUMP"
docker exec "$POSTGRES_ID" rm -f "$REMOTE_DUMP"

ACCOUNT_COUNT="$(docker exec "$POSTGRES_ID" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SELECT COUNT(*) FROM accounts')"
LONG_TEXT_COUNT="$(docker exec "$POSTGRES_ID" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SELECT COUNT(*) FROM long_texts')"
RAG_CHUNK_COUNT="$(docker exec "$POSTGRES_ID" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SELECT COUNT(*) FROM rag_chunks')"
ALEMBIC_REVISION="$(docker exec "$POSTGRES_ID" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SELECT version_num FROM alembic_version')"
OBJECT_COUNT="$(docker exec "$MINIO_ID" sh -ec 'mc alias set recovery-source http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null; mc ls --recursive --json "recovery-source/$1" | wc -l' sh "$OBJECT_BUCKET")"
[[ "$ACCOUNT_COUNT" =~ ^[0-9]+$ && "$LONG_TEXT_COUNT" =~ ^[0-9]+$ && "$RAG_CHUNK_COUNT" =~ ^[0-9]+$ && "$OBJECT_COUNT" =~ ^[0-9]+$ ]] || {
  echo "Snapshot counters are invalid." >&2
  exit 1
}
[[ "$ALEMBIC_REVISION" =~ ^[A-Za-z0-9_.-]+$ ]] || {
  echo "Alembic revision is invalid." >&2
  exit 1
}

compose_production stop minio
docker run --rm --entrypoint sh \
  -v "${MINIO_VOLUME}:/data:ro" \
  "$POSTGRES_IMAGE" \
  -ec 'tar czf - -C /data .' > "$MINIO_ARCHIVE"

POSTGRES_SHA256="$(sha256sum "$POSTGRES_DUMP" | awk '{print $1}')"
MINIO_SHA256="$(sha256sum "$MINIO_ARCHIVE" | awk '{print $1}')"
python3 - "$MANIFEST_PATH" <<PY
import json
import sys

manifest = {
    "schema_version": 2,
    "created_at": "${STARTED_AT}",
    "compose_project": "${PROJECT_NAME}",
    "release": "$(basename "$RELEASE_DIR")",
    "topology": "${TOPOLOGY}",
    "postgres_database": "${POSTGRES_DB}",
    "postgres_user": "${POSTGRES_USER}",
    "postgres_dump": "postgres.dump",
    "minio_archive": "minio-data.tar.gz",
    "object_bucket": "${OBJECT_BUCKET}",
    "postgres_sha256": "${POSTGRES_SHA256}",
    "minio_sha256": "${MINIO_SHA256}",
    "account_count": int("${ACCOUNT_COUNT}"),
    "long_text_count": int("${LONG_TEXT_COUNT}"),
    "rag_chunk_count": int("${RAG_CHUNK_COUNT}"),
    "object_count": int("${OBJECT_COUNT}"),
    "alembic_revision": "${ALEMBIC_REVISION}",
    "redis_backup": False,
    "note": "Redis contains rebuildable queue and protection state and is intentionally excluded.",
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, ensure_ascii=True, indent=2)
    handle.write("\n")
PY
chmod 600 "$POSTGRES_DUMP" "$MINIO_ARCHIVE" "$MANIFEST_PATH"

echo "==> Leaving the production maintenance window"
compose_production up -d --no-build
wait_for_production_service postgres
wait_for_production_service minio
wait_for_production_service web
wait_for_production_service worker
wait_for_production_service beat
if [[ "$TOPOLOGY" == "coexist" ]]; then
  wait_for_production_service coexist-https
else
  wait_for_production_service reverse-proxy
fi
PRODUCTION_QUIESCED=0

echo "==> Verifying backup hashes before isolated restore"
[[ "$(sha256sum "$POSTGRES_DUMP" | awk '{print $1}')" == "$POSTGRES_SHA256" ]]
[[ "$(sha256sum "$MINIO_ARCHIVE" | awk '{print $1}')" == "$MINIO_SHA256" ]]

echo "==> Creating isolated recovery containers and volumes"
ISOLATED_CREATED=1
docker network create "$RECOVERY_NETWORK" >/dev/null
docker volume create "$RECOVERY_POSTGRES_VOLUME" >/dev/null
docker volume create "$RECOVERY_MINIO_VOLUME" >/dev/null

RECOVERY_POSTGRES_PASSWORD="$(openssl rand -hex 24)"
RECOVERY_MINIO_USER="recoveryadmin"
RECOVERY_MINIO_PASSWORD="$(openssl rand -hex 24)"

docker run -d \
  --name "$RECOVERY_POSTGRES" \
  --network "$RECOVERY_NETWORK" \
  -e "POSTGRES_USER=${POSTGRES_USER}" \
  -e "POSTGRES_PASSWORD=${RECOVERY_POSTGRES_PASSWORD}" \
  -e "POSTGRES_DB=${POSTGRES_DB}" \
  -v "${RECOVERY_POSTGRES_VOLUME}:/var/lib/postgresql/data" \
  "$POSTGRES_IMAGE" >/dev/null
wait_for_container "$RECOVERY_POSTGRES" "pg_isready -U '${POSTGRES_USER}' -d '${POSTGRES_DB}'"

docker cp "$POSTGRES_DUMP" "${RECOVERY_POSTGRES}:/tmp/production.dump"
docker exec "$RECOVERY_POSTGRES" pg_restore \
  --clean --if-exists --exit-on-error --no-owner --no-acl \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB" /tmp/production.dump
docker exec "$RECOVERY_POSTGRES" rm -f /tmp/production.dump

docker run --rm --entrypoint sh \
  -v "${RECOVERY_MINIO_VOLUME}:/data" \
  -v "${BACKUP_DIR}:/backup:ro" \
  "$POSTGRES_IMAGE" \
  -ec 'tar xzf /backup/minio-data.tar.gz -C /data'
docker run -d \
  --name "$RECOVERY_MINIO" \
  --network "$RECOVERY_NETWORK" \
  -e "MINIO_ROOT_USER=${RECOVERY_MINIO_USER}" \
  -e "MINIO_ROOT_PASSWORD=${RECOVERY_MINIO_PASSWORD}" \
  -v "${RECOVERY_MINIO_VOLUME}:/data" \
  "$MINIO_IMAGE" server /data >/dev/null
wait_for_container "$RECOVERY_MINIO" "curl --fail --silent http://127.0.0.1:9000/minio/health/live"

RESTORED_ACCOUNT_COUNT="$(docker exec "$RECOVERY_POSTGRES" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SELECT COUNT(*) FROM accounts')"
RESTORED_LONG_TEXT_COUNT="$(docker exec "$RECOVERY_POSTGRES" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SELECT COUNT(*) FROM long_texts')"
RESTORED_RAG_CHUNK_COUNT="$(docker exec "$RECOVERY_POSTGRES" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SELECT COUNT(*) FROM rag_chunks')"
RESTORED_ALEMBIC_REVISION="$(docker exec "$RECOVERY_POSTGRES" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SELECT version_num FROM alembic_version')"
RESTORED_OBJECT_COUNT="$(docker exec "$RECOVERY_MINIO" sh -ec 'mc alias set recovery-target http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null; mc stat "recovery-target/$1" >/dev/null; mc ls --recursive --json "recovery-target/$1" | wc -l' sh "$OBJECT_BUCKET")"

[[ "$RESTORED_ACCOUNT_COUNT" == "$ACCOUNT_COUNT" ]]
[[ "$RESTORED_LONG_TEXT_COUNT" == "$LONG_TEXT_COUNT" ]]
[[ "$RESTORED_RAG_CHUNK_COUNT" == "$RAG_CHUNK_COUNT" ]]
[[ "$RESTORED_ALEMBIC_REVISION" == "$ALEMBIC_REVISION" ]]
[[ "$RESTORED_OBJECT_COUNT" == "$OBJECT_COUNT" ]]

COMPLETED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RTO_SECONDS=$(( $(date +%s) - STARTED_EPOCH ))
python3 - "$REPORT_PATH" <<PY
import json
import sys

report = {
    "result": "PASSED",
    "started_at": "${STARTED_AT}",
    "completed_at": "${COMPLETED_AT}",
    "recovery_time_objective_observed_seconds": int("${RTO_SECONDS}"),
    "operational_rpo_measured": False,
    "production_data_modified": False,
    "backup_directory": "${BACKUP_DIR}",
    "checks": {
        "postgres_sha256_verified": True,
        "minio_sha256_verified": True,
        "account_count_matches": True,
        "long_text_count_matches": True,
        "rag_chunk_count_matches": True,
        "object_count_matches": True,
        "alembic_revision_matches": True,
        "isolated_restore_used_unique_volumes": True,
    },
    "note": "The restore target used isolated containers and volumes. Production RPO still depends on backup frequency and off-host replication.",
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(report, handle, ensure_ascii=True, indent=2)
    handle.write("\n")
PY
chmod 600 "$REPORT_PATH"

echo "Production backup and isolated restore validation: PASS"
echo "Backup directory: ${BACKUP_DIR}"
echo "Observed recovery drill duration: ${RTO_SECONDS}s"
