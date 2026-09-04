#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

if [[ $# -ne 2 || "$2" != "BACKUP" ]]; then
  echo "Usage: run_production_backup.sh <app-root> BACKUP" >&2
  exit 2
fi

APP_ROOT="$1"
PROJECT_NAME="job-hunting-agent-production"
CURRENT_LINK="${APP_ROOT}/current"
SHARED_ENV="${APP_ROOT}/shared/.env"
BACKUP_ENV="${APP_ROOT}/shared/backup.env"
STATE_DIR="${APP_ROOT}/state"
BACKUP_ROOT="${APP_ROOT}/backups/scheduled"
LOCK_DIR="${STATE_DIR}/production-operation.lock"
STATUS_PATH="${STATE_DIR}/last-scheduled-backup.json"

[[ "$APP_ROOT" =~ ^/[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)+$ ]] || {
  echo "Invalid application root: ${APP_ROOT}" >&2
  exit 2
}

for command_name in curl date docker openssl python3 readlink sha256sum stat; do
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
[[ -f "$BACKUP_ENV" ]] || {
  echo "Backup environment file is missing: ${BACKUP_ENV}" >&2
  exit 1
}
[[ "$(stat -c '%a' "$BACKUP_ENV")" == "600" ]] || {
  echo "Backup environment file must use mode 600: ${BACKUP_ENV}" >&2
  exit 1
}
for state_file in current-image current-topology; do
  [[ -f "${STATE_DIR}/${state_file}" ]] || {
    echo "Current deployment state is missing: ${state_file}" >&2
    exit 1
  }
done

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
chmod 700 "$STATE_DIR" "$BACKUP_ROOT"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "Another production data operation is active: ${LOCK_DIR}" >&2
  exit 1
fi

STAMP="$(date -u +%Y%m%d-%H%M%S)"
SUFFIX="$(openssl rand -hex 6)"
BACKUP_ID="${STAMP}-${SUFFIX}"
PARTIAL_DIR="${BACKUP_ROOT}/.${BACKUP_ID}.partial"
BACKUP_DIR="${BACKUP_ROOT}/${BACKUP_ID}"
POSTGRES_DUMP="${PARTIAL_DIR}/postgres.dump"
MINIO_ARCHIVE="${PARTIAL_DIR}/minio-data.tar.gz"
MANIFEST_PATH="${PARTIAL_DIR}/manifest.json"
UPLOAD_REPORT="${BACKUP_DIR}/offsite-upload.json"
PRODUCTION_QUIESCED=0
FAILED_LINE=0
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

record_failure_line() {
  FAILED_LINE="$1"
}
trap 'record_failure_line "$LINENO"' ERR

write_status() {
  local result="$1"
  local completed_at="$2"
  local offsite_status="$3"
  local temporary="${STATUS_PATH}.tmp"
  BACKUP_RESULT="$result" \
    BACKUP_ID_VALUE="$BACKUP_ID" \
    BACKUP_STARTED_AT="$STARTED_AT" \
    BACKUP_COMPLETED_AT="$completed_at" \
    BACKUP_OFFSITE_STATUS="$offsite_status" \
    BACKUP_FAILED_LINE="$FAILED_LINE" \
    python3 - "$temporary" <<'PY'
import json
import os
import sys

payload = {
    "schema_version": 1,
    "result": os.environ["BACKUP_RESULT"],
    "backup_id": os.environ["BACKUP_ID_VALUE"],
    "started_at": os.environ["BACKUP_STARTED_AT"],
    "completed_at": os.environ["BACKUP_COMPLETED_AT"],
    "offsite_status": os.environ["BACKUP_OFFSITE_STATUS"],
    "failed_line": int(os.environ["BACKUP_FAILED_LINE"]),
    "target_rpo_hours": 24,
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=True, indent=2)
    handle.write("\n")
PY
  chmod 600 "$temporary"
  mv "$temporary" "$STATUS_PATH"
}

post_backup_alert() {
  local state="$1"
  local alertmanager_id port ends_at summary description payload

  alertmanager_id="$(compose_production ps -q alertmanager 2>/dev/null || true)"
  [[ -n "$alertmanager_id" ]] || return 0
  port="$(docker port "$alertmanager_id" 9093/tcp 2>/dev/null | head -n 1 | awk -F: '{print $NF}')"
  [[ "$port" =~ ^[0-9]+$ ]] || return 0
  if [[ "$state" == "firing" ]]; then
    ends_at="$(date -u -d '+24 hours' +%Y-%m-%dT%H:%M:%SZ)"
    summary="Job Hunting Agent scheduled backup failed"
    description="Backup ${BACKUP_ID} failed. Inspect systemctl status job-agent-backup.service and ${STATUS_PATH}."
  else
    ends_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    summary="Job Hunting Agent scheduled backup recovered"
    description="Backup ${BACKUP_ID} completed successfully."
  fi
  payload="$({
    ALERT_ENDS_AT="$ends_at" \
      ALERT_SUMMARY="$summary" \
      ALERT_DESCRIPTION="$description" \
      python3 - <<'PY'
import json
import os

print(json.dumps([{
    "labels": {
        "alertname": "JobAgentScheduledBackupFailed",
        "environment": "production",
        "service": "backup",
        "severity": "critical",
    },
    "annotations": {
        "summary": os.environ["ALERT_SUMMARY"],
        "description": os.environ["ALERT_DESCRIPTION"],
    },
    "endsAt": os.environ["ALERT_ENDS_AT"],
}], ensure_ascii=True))
PY
  })"
  curl --fail --silent --show-error --max-time 10 \
    -H 'Content-Type: application/json' \
    --data-binary "$payload" \
    "http://127.0.0.1:${port}/api/v2/alerts" \
    >/dev/null || true
}

cleanup() {
  local exit_code=$?
  local restart_failed=0
  set +e

  if (( PRODUCTION_QUIESCED == 1 )); then
    echo "==> Restoring production services"
    if ! compose_production up -d --no-build; then
      echo "CRITICAL: automatic production service restart failed." >&2
      restart_failed=1
    fi
  fi
  if (( exit_code != 0 || restart_failed == 1 )); then
    write_status "FAILED" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "FAILED" || true
    post_backup_alert firing
    if [[ -d "$PARTIAL_DIR" && "$(readlink -f "$PARTIAL_DIR")" == "${BACKUP_ROOT}/"* ]]; then
      rm -rf --one-file-system -- "$PARTIAL_DIR"
    fi
  fi
  rmdir "$LOCK_DIR" >/dev/null 2>&1 || true
  if (( restart_failed == 1 )); then
    exit 1
  fi
  exit "$exit_code"
}
trap cleanup EXIT

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

read_image_setting() {
  local name="$1"
  local default_value="$2"
  docker run --rm --env-file "$BACKUP_ENV" "$IMAGE_REF" \
    python -c 'import os, sys; print(os.environ.get(sys.argv[1], sys.argv[2]))' \
    "$name" "$default_value"
}

prune_local_backups() {
  local keep="$1"
  local index directory name
  local -a directories=()

  mapfile -t directories < <(
    find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
      -name '20??????-??????-????????????' -printf '%f\n' | sort -r
  )
  for (( index=keep; index<${#directories[@]}; index++ )); do
    name="${directories[$index]}"
    [[ "$name" =~ ^20[0-9]{6}-[0-9]{6}-[0-9a-f]{12}$ ]] || continue
    directory="${BACKUP_ROOT}/${name}"
    [[ -f "${directory}/COMPLETE" ]] || continue
    [[ "$(readlink -f "$directory")" == "${BACKUP_ROOT}/"* ]] || continue
    rm -rf --one-file-system -- "$directory"
  done
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
MINIO_VOLUME="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$MINIO_ID")"
POSTGRES_USER="$(docker exec "$POSTGRES_ID" printenv POSTGRES_USER)"
POSTGRES_DB="$(docker exec "$POSTGRES_ID" printenv POSTGRES_DB)"
OBJECT_BUCKET="$(docker exec "$WEB_ID" printenv JOB_AGENT_OBJECT_STORAGE_BUCKET)"
[[ -n "$POSTGRES_IMAGE" && -n "$MINIO_VOLUME" ]] || {
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
REQUIRED_BYTES=$((DATABASE_BYTES + MINIO_BYTES * 2 + 268435456))
AVAILABLE_BYTES=$((AVAILABLE_KB * 1024))
if (( AVAILABLE_BYTES < REQUIRED_BYTES )); then
  echo "Insufficient disk space. Required=${REQUIRED_BYTES}, available=${AVAILABLE_BYTES}." >&2
  exit 1
fi

mkdir "$PARTIAL_DIR"
chmod 700 "$PARTIAL_DIR"

echo "==> Entering a brief production maintenance window"
PRODUCTION_QUIESCED=1
if [[ "$TOPOLOGY" == "coexist" ]]; then
  compose_production stop coexist-https
else
  compose_production stop reverse-proxy
fi
compose_production stop web worker beat

REMOTE_DUMP="/tmp/job-agent-${BACKUP_ID}.dump"
docker exec "$POSTGRES_ID" pg_dump \
  --format=custom --no-owner --no-acl \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$REMOTE_DUMP"
docker cp "${POSTGRES_ID}:${REMOTE_DUMP}" "$POSTGRES_DUMP"
docker exec "$POSTGRES_ID" rm -f "$REMOTE_DUMP"

ACCOUNT_COUNT="$(docker exec "$POSTGRES_ID" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SELECT COUNT(*) FROM accounts')"
LONG_TEXT_COUNT="$(docker exec "$POSTGRES_ID" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SELECT COUNT(*) FROM long_texts')"
RAG_CHUNK_COUNT="$(docker exec "$POSTGRES_ID" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SELECT COUNT(*) FROM rag_chunks')"
ALEMBIC_REVISION="$(docker exec "$POSTGRES_ID" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SELECT version_num FROM alembic_version')"
OBJECT_COUNT="$(docker exec "$MINIO_ID" sh -ec 'mc alias set backup-source http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null; mc ls --recursive --json "backup-source/$1" | wc -l' sh "$OBJECT_BUCKET")"
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
COMPLETED_SNAPSHOT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - "$MANIFEST_PATH" <<PY
import json
import sys

manifest = {
    "schema_version": 3,
    "backup_id": "${BACKUP_ID}",
    "created_at": "${STARTED_AT}",
    "snapshot_completed_at": "${COMPLETED_SNAPSHOT_AT}",
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
    "target_rpo_hours": 24,
    "redis_backup": False,
    "note": "Redis contains rebuildable queue and protection state and is excluded.",
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, ensure_ascii=True, indent=2)
    handle.write("\n")
PY
chmod 600 "$POSTGRES_DUMP" "$MINIO_ARCHIVE" "$MANIFEST_PATH"

echo "==> Leaving the production maintenance window"
compose_production up -d --no-build
for service in postgres minio web worker beat; do
  wait_for_production_service "$service"
done
if [[ "$TOPOLOGY" == "coexist" ]]; then
  wait_for_production_service coexist-https
else
  wait_for_production_service reverse-proxy
fi
PRODUCTION_QUIESCED=0

[[ "$(sha256sum "$POSTGRES_DUMP" | awk '{print $1}')" == "$POSTGRES_SHA256" ]]
[[ "$(sha256sum "$MINIO_ARCHIVE" | awk '{print $1}')" == "$MINIO_SHA256" ]]
touch "${PARTIAL_DIR}/COMPLETE"
chmod 600 "${PARTIAL_DIR}/COMPLETE"
mv "$PARTIAL_DIR" "$BACKUP_DIR"

OFFSITE_ENABLED="$(read_image_setting JOB_AGENT_BACKUP_OFFSITE_ENABLED false)"
OFFSITE_ENABLED="${OFFSITE_ENABLED,,}"
OFFSITE_STATUS="DISABLED"
case "$OFFSITE_ENABLED" in
  1 | true | yes | on) OFFSITE_ENABLED=true ;;
  0 | false | no | off) OFFSITE_ENABLED=false ;;
  *)
    echo "JOB_AGENT_BACKUP_OFFSITE_ENABLED must be true or false." >&2
    exit 1
    ;;
esac
if [[ "$OFFSITE_ENABLED" == "true" ]]; then
  echo "==> Uploading and verifying the encrypted offsite backup"
  temporary_upload_report="${UPLOAD_REPORT}.tmp"
  docker run --rm --network host \
    --user "$(id -u):$(id -g)" \
    --env-file "$BACKUP_ENV" \
    -v "${BACKUP_DIR}:/backup:ro" \
    "$IMAGE_REF" \
    python -m job_hunting_agent.backup_storage upload \
      --directory /backup \
      --backup-id "$BACKUP_ID" \
    > "$temporary_upload_report"
  chmod 600 "$temporary_upload_report"
  mv "$temporary_upload_report" "$UPLOAD_REPORT"
  OFFSITE_STATUS="VERIFIED"
fi

LOCAL_RETENTION="$(read_image_setting JOB_AGENT_BACKUP_LOCAL_RETENTION_COUNT 7)"
[[ "$LOCAL_RETENTION" =~ ^[0-9]+$ ]] && (( LOCAL_RETENTION >= 2 && LOCAL_RETENTION <= 90 )) || {
  echo "JOB_AGENT_BACKUP_LOCAL_RETENTION_COUNT must be between 2 and 90." >&2
  exit 1
}
prune_local_backups "$LOCAL_RETENTION"

write_status "PASSED" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$OFFSITE_STATUS"
post_backup_alert resolved
echo "Production scheduled backup: PASS"
echo "Backup directory: ${BACKUP_DIR}"
echo "Offsite status: ${OFFSITE_STATUS}"
