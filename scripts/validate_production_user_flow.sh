#!/usr/bin/env bash
set -Eeuo pipefail

BASE_URL="${1:-}"
CONFIRMATION="${2:-}"
DEPLOY_ROOT="${JOB_AGENT_DEPLOY_ROOT:-/opt/job-hunting-agent}"
WEB_CONTAINER="${JOB_AGENT_WEB_CONTAINER:-job-hunting-agent-production-web-1}"
LOCK_DIR="${DEPLOY_ROOT}/state/production-operation.lock"
VALIDATOR="${DEPLOY_ROOT}/current/scripts/validate_production_user_flow.py"

if [[ -z "${BASE_URL}" || "${CONFIRMATION}" != "RUN" ]]; then
  echo "Usage: $0 https://PUBLIC_HOST[:PORT] RUN" >&2
  exit 2
fi

if [[ ! -f "${VALIDATOR}" ]]; then
  echo "Production user-flow validator not found: ${VALIDATOR}" >&2
  exit 2
fi

if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  echo "Another production operation is active: ${LOCK_DIR}" >&2
  exit 1
fi
trap 'rmdir "${LOCK_DIR}"' EXIT HUP INT TERM

docker exec -i "${WEB_CONTAINER}" python - \
  --base-url "${BASE_URL}" \
  --confirmation RUN_PRODUCTION_USER_FLOW \
  --timeout-seconds 180 \
  < "${VALIDATOR}"
