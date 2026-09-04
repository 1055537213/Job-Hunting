#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

if [[ $# -lt 3 || $# -gt 4 || "$3" != "INSTALL" ]]; then
  echo "Usage: install_production_backup_timer.sh <app-root> <service-user> INSTALL [on-calendar]" >&2
  exit 2
fi
if (( EUID != 0 )); then
  echo "This installer must run as root." >&2
  exit 1
fi

APP_ROOT="$1"
SERVICE_USER="$2"
ON_CALENDAR="${4:-*-*-* 03:30:00 Asia/Shanghai}"
SERVICE_NAME="job-agent-backup.service"
TIMER_NAME="job-agent-backup.timer"

[[ "$APP_ROOT" =~ ^/[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)+$ ]] || {
  echo "Invalid application root: ${APP_ROOT}" >&2
  exit 2
}
[[ "$SERVICE_USER" =~ ^[A-Za-z_][A-Za-z0-9._-]*$ ]] || {
  echo "Invalid service user: ${SERVICE_USER}" >&2
  exit 2
}
[[ "$ON_CALENDAR" =~ ^[A-Za-z0-9*,:./\ _-]+$ ]] || {
  echo "OnCalendar contains unsupported characters." >&2
  exit 2
}
id "$SERVICE_USER" >/dev/null 2>&1 || {
  echo "Service user does not exist: ${SERVICE_USER}" >&2
  exit 1
}
id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -Fx docker >/dev/null || {
  echo "Service user must belong to the docker group." >&2
  exit 1
}
[[ -x "${APP_ROOT}/current/scripts/run_production_backup.sh" ]] || {
  echo "Deployed backup script is missing or not executable." >&2
  exit 1
}
[[ -f "${APP_ROOT}/shared/backup.env" ]] || {
  echo "Create ${APP_ROOT}/shared/backup.env from deploy/backup.env.example first." >&2
  exit 1
}

SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 700 \
  "${APP_ROOT}/state" "${APP_ROOT}/backups" "${APP_ROOT}/backups/scheduled"
chown "$SERVICE_USER:$SERVICE_GROUP" "${APP_ROOT}/shared/backup.env"
chmod 600 "${APP_ROOT}/shared/backup.env"

cat > "/etc/systemd/system/${SERVICE_NAME}" <<EOF
[Unit]
Description=Job Hunting Agent verified production backup
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
UMask=0077
ExecStart=${APP_ROOT}/current/scripts/run_production_backup.sh ${APP_ROOT} BACKUP
TimeoutStartSec=2h
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
EOF

cat > "/etc/systemd/system/${TIMER_NAME}" <<EOF
[Unit]
Description=Run the Job Hunting Agent production backup every day

[Timer]
OnCalendar=${ON_CALENDAR}
RandomizedDelaySec=15m
Persistent=true
Unit=${SERVICE_NAME}

[Install]
WantedBy=timers.target
EOF

chmod 644 "/etc/systemd/system/${SERVICE_NAME}" "/etc/systemd/system/${TIMER_NAME}"
systemctl daemon-reload
systemctl enable --now "$TIMER_NAME"
systemctl list-timers "$TIMER_NAME" --no-pager
echo "Installed ${TIMER_NAME} with OnCalendar=${ON_CALENDAR}"
