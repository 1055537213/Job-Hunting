# 生产发布与恢复基线

本项目当前采用单机生产拓扑，不要求 Kubernetes。生产配置和开发配置分开：

- `compose.yaml`：基础服务定义和本地可复现镜像运行方式。
- `compose.dev.yaml`：仅供本地开发，包含源码挂载和 Web 热更新。
- `compose.prod.yaml`：生产覆盖配置，移除宿主机服务端口，使用独立生产数据卷，并通过 Caddy 暴露 HTTPS。

## 首次准备

1. 复制 `.env.example`，填入模型、对象存储和业务配置。
2. 用 `deploy/env.production.example` 中的生产部署项覆盖对应变量。
3. 设置 `JOB_AGENT_IMAGE` 为 CI 已构建的不可变镜像标签。
4. 使用 URL-safe 字符生成 `JOB_AGENT_POSTGRES_PASSWORD`，避免连接 URL 解析歧义。
5. 在 MinIO 或托管 S3 中预先创建 bucket，并确认 `JOB_AGENT_OBJECT_STORAGE_AUTO_CREATE_BUCKET=false`。

生产 `.env` 只保存于服务器受限目录，不提交 Git，不复制进镜像。

## 发布

```powershell
docker build --tag $env:JOB_AGENT_IMAGE .
docker compose -f compose.yaml -f compose.prod.yaml config --quiet
docker compose -f compose.yaml -f compose.prod.yaml up -d --no-build
docker compose -f compose.yaml -f compose.prod.yaml ps
```

发布顺序由 Compose 保证：PostgreSQL 健康后执行 Alembic，迁移成功后才启动 Web 和 Worker，
Web 健康后 Caddy 才接收外部流量。

## 备份

备份脚本会在线导出 PostgreSQL，并在短暂维护窗口中归档 MinIO 数据卷：

```powershell
.\scripts\backup.ps1
```

备份目录包含：

- `postgres.dump`：PostgreSQL custom-format 逻辑备份。
- `minio-data.tar.gz`：MinIO 对象数据卷归档。
- `manifest.json`：创建时间、SHA-256 和备份说明。

Redis 不进入备份。它只承载可重建的队列和缓存，任务权威状态在 PostgreSQL 中。
生产环境应把 `data/backups` 同步到独立存储，并设置保留周期。

## 恢复演练

恢复会覆盖生产数据库和对象存储，必须显式确认：

```powershell
.\scripts\restore.ps1 -BackupDirectory .\data\backups\20260822-120000 -ConfirmRestore
```

恢复后脚本会重新执行迁移并启动完整生产拓扑。正式上线前至少完成一次恢复演练，记录：

- RPO：最多允许丢失多久的数据。
- RTO：从故障到服务恢复需要多久。
- 数据库记录和对象文件是否都能通过账号归属校验。
- 未完成任务是否按照 PostgreSQL 状态恢复或重新投递。

脚本默认会在完成后重新启动服务；排障时可使用 `-KeepServicesStopped` 保留停机状态。
