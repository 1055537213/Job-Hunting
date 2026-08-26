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

7. 确认生产 Web 和 Worker 使用 PostgreSQL 共享 Agent 记忆：
   `JOB_AGENT_MEMORY_CHECKPOINT_BACKEND=database`。生产禁止使用 `memory`，因为它只保存在
   单个进程内；当 Web 扩容、重启或请求切换到另一副本时，进程内记忆会导致同一会话上下文不一致。

8. 设置远程模型调用的有限重试和熔断参数。以下配置同时作为 Chat、Embedding 和 Rerank
   的熔断基线，再根据真实容量和供应商 SLA 调整：

   ```dotenv
   JOB_AGENT_MODEL_GATEWAY_CHAT_MAX_RETRIES=2
   JOB_AGENT_MODEL_CIRCUIT_FAILURE_THRESHOLD=5
   JOB_AGENT_MODEL_CIRCUIT_RECOVERY_SECONDS=30
   ```

   `CHAT_MAX_RETRIES` 控制一次聊天请求遇到可恢复上游错误时的有限重试次数；
   `EMBEDDING_MAX_RETRIES` 和 `RERANK_MAX_RETRIES` 分别控制向量请求和重排请求。
   连续达到 `FAILURE_THRESHOLD` 后，当前 Web 进程会暂时拒绝对应的远程模型调用，等待
   `RECOVERY_SECONDS` 后放行一次探测。超时、连接失败、429 和 5xx 会触发熔断；鉴权失败、
   参数错误和余额不足不会触发熔断。熔断期间 Web 返回 HTTP 503，并附带 `Retry-After`，
   客户端应稍后重试。

9. 保持后台任务失联回收时间大于 Celery 硬超时。例如默认配置为：

   ```dotenv
   JOB_AGENT_TASK_TIME_LIMIT_SECONDS=900
   JOB_AGENT_TASK_SOFT_TIME_LIMIT_SECONDS=840
   JOB_AGENT_TASK_STALE_AFTER_SECONDS=1800
   ```

   Worker 使用 late acknowledgement 和 `reject_on_worker_lost`；如果 Worker 在任务执行
   中崩溃，Beat 会回收数据库中超时的 `running` 任务并重新投递同一个 `task_key`。达到
   `max_attempts` 后任务会进入 `failed`，不会无限重试。该回收机制不替代上线前的真实
   进程崩溃、消息重投和容量演练。

   Beat 的回收和流水裁剪会投递到 `<JOB_AGENT_TASK_QUEUE_NAME>_maintenance`。普通 Worker
   默认同时消费业务队列和这个维护队列；若单独启动维护 Worker，可使用
   `job-agent-worker --queue <JOB_AGENT_TASK_QUEUE_NAME>_maintenance`。

10. 配置生产文件安全扫描。生产环境禁止使用本地占位扫描器，Compose 会启动 ClamAV
   并把 `clamav` 服务作为 Web/Worker 的健康依赖：

   ```dotenv
   JOB_AGENT_FILE_SCAN_BACKEND=clamav
   JOB_AGENT_FILE_SCAN_HOST=clamav
   JOB_AGENT_FILE_SCAN_PORT=3310
   JOB_AGENT_FILE_SCAN_TIMEOUT_SECONDS=10
   ```

   简历、职位截图和公开 GitHub ZIP 归档在 OCR、解压、模型或 RAG 之前扫描。扫描通过后，
   简历记录的 `scan_status` 为 `clean`；感染文件或扫描服务异常会进入 `quarantined`，
   不会创建长文本、执行 OCR、进入 RAG 或提供下载。`scan_reason` 只保存低敏摘要，不保存
    文件正文。生产首次发布和病毒库更新后必须使用 EICAR 测试文件验证拦截链路，然后删除
    隔离对象并检查数据库记录是否仍可按账号归属清理。

   发布到目标服务器前执行独立验收：

   ```powershell
   .\scripts\validate_file_scanning.ps1
   ```

   脚本检查 daily 病毒库真实构建时间、正常文件、EICAR、扫描服务停机、恢复和 PostgreSQL +
   MinIO 清理。详细说明见 [ClamAV 文件扫描验收](file-scanning-acceptance.md)。

11. 若生产环境需要项目图片和 PDF 页参与跨模态召回，Embedding 必须使用原生多模态协议；
    普通 OpenAI-compatible 文本 Embedding 只会保留文字 RAG：

    ```dotenv
    JOB_AGENT_EMBEDDING_PROVIDER=dashscope
    JOB_AGENT_EMBEDDING_API_STYLE=native_multimodal
    JOB_AGENT_EMBEDDING_MODEL=qwen3-vl-embedding
    JOB_AGENT_EMBEDDING_API_KEY=<production-secret>
    JOB_AGENT_EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding
    JOB_AGENT_EMBEDDING_DIMENSIONS=1024
    ```

    发布后导入一个包含图片或 PDF 的测试项目，确认 `visual_knowledge_items` 从 `pending` 进入
    `indexed`，并用只描述视觉内容的文字问题验证能够命中对应图片或 PDF 页。检索会限量重开
    前两项命中原图进行查询相关复核，因此测试账号还必须有可用余额，并应在 Token/消费流水中
    看到 `project_visual_reinspection`。测试数据完成后按正常项目删除流程清理，以同时验证对象存储、
    数据库和向量生命周期。

12. 配置生产账号邮件和协议版本。生产启动会拒绝 `console` 邮件后端、HTTP 公开地址，
    或关闭邮箱验证/协议同意的配置：

    ```dotenv
    JOB_AGENT_EMAIL_VERIFICATION_REQUIRED=true
    JOB_AGENT_CONSENT_REQUIRED=true
    JOB_AGENT_PUBLIC_BASE_URL=https://agent.example.com
    JOB_AGENT_ACCOUNT_EMAIL_BACKEND=smtp
    JOB_AGENT_ACCOUNT_ACTION_SECRET=<at-least-32-random-characters>
    JOB_AGENT_ACCOUNT_EMAIL_COOLDOWN_SECONDS=60
    JOB_AGENT_ACCOUNT_EMAIL_HOURLY_LIMIT=5
    JOB_AGENT_ACCOUNT_EMAIL_SOURCE_HOURLY_LIMIT=20
    JOB_AGENT_ACCOUNT_EMAIL_MAX_ATTEMPTS=5
    JOB_AGENT_ACCOUNT_EMAIL_RETRY_BASE_SECONDS=30
    JOB_AGENT_ACCOUNT_EMAIL_CLAIM_TIMEOUT_SECONDS=300
    JOB_AGENT_ACCOUNT_EMAIL_RETENTION_DAYS=14
    JOB_AGENT_SMTP_HOST=smtp.example.com
    JOB_AGENT_SMTP_PORT=587
    JOB_AGENT_SMTP_USERNAME=<production-secret>
    JOB_AGENT_SMTP_PASSWORD=<production-secret>
    JOB_AGENT_SMTP_FROM_EMAIL=no-reply@example.com
    JOB_AGENT_TERMS_VERSION=2026-08-26
    JOB_AGENT_PRIVACY_VERSION=2026-08-26
    ```

    `JOB_AGENT_ACCOUNT_ACTION_SECRET` 可用
    `python -c "import secrets; print(secrets.token_urlsafe(48))"` 生成，不能提交到仓库。
    Web 只把邮件和一次性令牌哈希登记到 PostgreSQL，Celery 消息只携带 Outbox ID；Worker
    负责 SMTP，失败按指数退避，Beat 每 30 秒补投到期或失联记录。后台“请求观测”页只显示
    遮盖邮箱、状态、尝试次数和固定失败摘要，终态记录默认保留 14 天。

    发布前使用真实收件箱验证注册、重发验证邮件和忘记密码三条链路，并确认一次性链接在
    使用后或过期后不能再次使用。还要临时阻断 SMTP，确认 Web 请求仍能成功返回、记录进入
    `retrying`，恢复 SMTP 后最终进入 `sent`。SMTP 在“服务端已接收、Worker 尚未写回成功”时
    无法提供严格的端到端 exactly-once，邮件内容应保持幂等且允许极少量重复送达。协议正文和
    版本号必须由实际运营主体审核，版本更新时只修改版本号不足以替代重新获取用户同意。

生产覆盖不会向宿主机发布 Web 端口，只有 Caddy 和内部采集服务可以访问它，因此
`FORWARDED_ALLOW_IPS=*` 只在该覆盖配置中启用。不要把这个设置复制到直接暴露 Uvicorn 的开发环境。

生产 `.env` 只保存于服务器受限目录，不提交 Git，不复制进镜像。

## 发布

构建生产镜像前先执行供应链门禁：

```powershell
.\scripts\security_scan.ps1
```

确认 `python_gate_passed` 和 `container_gate_passed` 均为 `true`，并将本次 CycloneDX SBOM 与
发布记录关联。详细策略见 [依赖与容器镜像安全扫描](security-scanning.md)。

```powershell
docker build --pull --tag $env:JOB_AGENT_IMAGE .
docker compose -f compose.yaml -f compose.prod.yaml config --quiet
docker compose -f compose.yaml -f compose.prod.yaml up -d --no-build
docker compose -f compose.yaml -f compose.prod.yaml ps
```

发布顺序由 Compose 保证：PostgreSQL 健康后执行 Alembic，ClamAV 健康后才启动 Web 和
Worker，迁移成功且 Web 健康后 Caddy 才接收外部流量。

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

上线前还要执行一次真实 Worker 故障恢复验收：

```powershell
.\scripts\validate_worker_recovery.ps1
```

该脚本临时叠加 `compose.acceptance.yaml`，把 Worker 的硬超时设为 30 秒、失联回收窗口
设为 60 秒，然后创建隔离的 `system_probe` 任务。在任务已经进入 `running` 后强制停止
Worker，等待 Beat 把任务从 `running` 原子回收为 `queued`，再启动同一个 Worker，检查任务
最终只成功一次。脚本还会重复提交同一幂等键，并比较 `background_tasks`、Token 用量、余额
流水、简历文件和工具轨迹数量，确保重复消息不会产生第二份业务副作用。验收账号在结束时
按账号级外键级联删除；成功或失败都会尝试恢复普通开发拓扑。

这项演练验证的是恢复链路，不等同于真实简历导出压力测试。上线前仍应使用接近生产大小的
文件和并发量，另外测量大文件、模型超时、Redis 重启以及多个 Worker 同时消费时的 RTO/RPO。

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
脚本默认操作 `job-hunting-agent-production` 项目；`-ProjectName` 与 `-ComposeFiles` 仅用于
隔离验收或显式指定的部署拓扑，日常生产备份不需要传入。

## 恢复演练

恢复会覆盖生产数据库和对象存储，必须显式确认：

```powershell
.\scripts\restore.ps1 -BackupDirectory .\data\backups\20260822-120000 -ConfirmRestore
```

恢复脚本要求目录中同时存在 `manifest.json`、`postgres.dump` 和 `minio-data.tar.gz`，并在
停止任何服务前校验清单版本、固定文件名和两个 SHA-256。任一文件缺失、清单损坏或哈希不符
都会终止恢复，不会进入数据库与对象卷覆盖步骤。

恢复后脚本会重新执行迁移并启动完整生产拓扑。正式上线前至少完成一次恢复演练，记录：

- RPO：最多允许丢失多久的数据。
- RTO：从故障到服务恢复需要多久。
- 数据库记录和对象文件是否都能通过账号归属校验。
- 未完成任务是否按照 PostgreSQL 状态恢复或重新投递。

脚本默认会在完成后重新启动服务；排障时可使用 `-KeepServicesStopped` 保留停机状态。

本地或 CI 前置环境可以执行不触碰开发数据的自动化演练：

```powershell
.\scripts\validate_backup_restore.ps1
```

该脚本通过 `compose.recovery-test.yaml` 移除宿主机端口，并为每次执行创建唯一 Compose 项目和
命名卷。它验证真实 PostgreSQL 记录、真实 MinIO 对象、归档篡改拒绝、备份后数据回滚和当前
Alembic revision，最后输出 `data/recovery-drills/<run>/recovery-report.json` 并删除隔离卷。
报告中的 `recovery_time_objective_observed_seconds` 是本机本次演练 RTO；
`operational_rpo_measured=false` 表示脚本不能代替按正式备份频率计算生产 RPO。
