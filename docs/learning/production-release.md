# 生产发布与恢复基线

本项目当前采用单机生产拓扑，不要求 Kubernetes。生产配置和开发配置分开：

- `compose.yaml`：基础服务定义和本地可复现镜像运行方式。
- `compose.dev.yaml`：仅供本地开发，包含源码挂载和 Web 热更新。
- `compose.prod.yaml`：生产覆盖配置，移除业务内部服务的公网端口，使用独立生产数据卷，
  通过 Caddy 暴露 HTTPS，并启动仅绑定服务器回环地址的 Prometheus。

## 首次准备

1. 复制 `.env.example`，填入模型、对象存储和业务配置。
2. 用 `deploy/env.production.example` 中的生产部署项覆盖对应变量。
3. 设置 `JOB_AGENT_IMAGE` 为 CI 已构建的不可变镜像标签。
4. 使用 URL-safe 字符生成 `JOB_AGENT_POSTGRES_PASSWORD`，避免连接 URL 解析歧义。
5. 在 MinIO 或托管 S3 中预先创建 bucket，并确认 `JOB_AGENT_OBJECT_STORAGE_AUTO_CREATE_BUCKET=false`。
6. 确认 Web 使用 `JOB_AGENT_RATE_LIMIT_BACKEND=redis`，并将限流 URL 指向 Redis 独立数据库 1。

生产覆盖不会向宿主机发布 Web 端口，只有 Caddy 和内部采集服务可以访问它，因此
`FORWARDED_ALLOW_IPS=*` 只在该覆盖配置中启用。不要把这个设置复制到直接暴露 Uvicorn 的开发环境。

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

## 指标和告警

Prometheus 每 15 秒从 Compose 内部的 Web 副本采集一次低敏请求指标，默认保留 15 天。
`dns_sd_configs` 每 5 秒查询 Docker DNS 的 `web` A 记录，因此每个 Web 容器都会成为独立
target；查询和告警按 `job="job-hunting-agent-web"` 聚合，排障时仍可用 `instance` 标签定位
单个副本。该端点只导出总量、路由模板、状态分组、耗时、并发数和安全拦截计数，不导出
请求正文、查询参数、账号信息、request ID 或最近错误详情。

Caddy 会对公网 `/internal/*` 请求直接返回 404。Prometheus 页面只监听服务器的
`127.0.0.1:9090`，可从运维电脑建立 SSH 端口转发后访问：

```powershell
ssh -L 9090:127.0.0.1:9090 <server-user>@<server-host>
```

浏览器打开 `http://127.0.0.1:9090/targets` 检查采集目标，打开
`http://127.0.0.1:9090/alerts` 检查告警规则。当前规则覆盖：

- Web 连续两分钟无法采集。
- 五分钟内至少五次 5xx，且错误比例持续高于 5%。
- 五分钟平均响应时间持续高于两秒。
- 十分钟内限流或 CSRF 拦截达到二十次，或 Redis 限流后端在五分钟内影响请求。
- 全部 Web 副本当前处理请求总数持续五分钟达到二十个。

规则文件位于 `deploy/prometheus/alerts.yml`。它们当前会在 Prometheus 中进入 pending 或
firing 状态，但不会自动向外发送通知；正式确定值班渠道后再接入 Alertmanager，避免把测试
告警发送到真实联系人。

配置修改后先校验：

```powershell
docker run --rm --entrypoint /bin/promtool `
  -v "${PWD}/deploy/prometheus:/etc/prometheus:ro" `
  prom/prometheus:v3.13.1 `
  check config /etc/prometheus/prometheus.yml
```

上线或修改反向代理、Redis 限流、指标采集后，执行真实双副本验收：

```powershell
.\scripts\validate_multi_replica.ps1
```

脚本临时应用 `compose.scale-test.yaml`，移除 Web 的宿主机固定端口并启动两个副本。它会直接
探测两个容器，交替发送认证失败请求以确认 Redis 限流额度跨进程共享，再确认 Prometheus
发现两个健康 target。无论通过还是失败，`finally` 都会恢复 `compose.dev.yaml` 的单 Web
拓扑和 `127.0.0.1:8000`；若恢复步骤警告失败，应立即手动执行开发 Compose 启动命令。

## 备份

备份脚本会在线导出 PostgreSQL，并在短暂维护窗口中归档 MinIO 数据卷：

```powershell
.\scripts\backup.ps1
```

备份目录包含：

- `postgres.dump`：PostgreSQL custom-format 逻辑备份。
- `minio-data.tar.gz`：MinIO 对象数据卷归档。
- `manifest.json`：创建时间、SHA-256 和备份说明。
- `prometheus_prod_data` 不属于业务事实备份；指标趋势超过保留期后可丢弃并重新采集。

Redis 不进入备份。它只承载可重建的队列和缓存，任务权威状态在 PostgreSQL 中。
请求限流窗口同样是带 TTL 的短期保护状态，Redis 恢复后会自动重新建立，不参与业务恢复。
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
