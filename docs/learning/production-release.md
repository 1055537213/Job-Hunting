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
    数据库和向量生命周期。生产环境还应按实际供应商响应时间设置
    `JOB_AGENT_PROJECT_VISUAL_BATCH_TIMEOUT_SECONDS` 与
    `JOB_AGENT_PROJECT_VISUAL_TOTAL_TIMEOUT_SECONDS`；超时必须能回退到 OCR/文字证据并完成任务收尾。

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

意图路由通过 `job_agent_intent_router_direct_total`、
`job_agent_intent_router_fallback_reasons_total`、`job_agent_intent_router_timeouts_total` 和
`job_agent_intent_router_model_duration_seconds` 观察直达率、回退原因、超时率与耗时分位数。
标签集合由代码固定，不包含用户消息、账号、prompt、工具参数或 request ID。

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

## RAG 召回质量门禁

固定黄金集位于 `evals/rag/golden_suite.json`，覆盖软件、工业图纸、公差数值、PDF 表格、
视觉摘要、医疗、教育、财务、物流、制造、法律、版本冲突和账号隔离。执行：

```powershell
.\scripts\validate_rag_retrieval.ps1 -Python E:\Anaconda\python.exe
```

脚本不读取现有用户知识库来凑测试数据。每次运行会在同一 PostgreSQL 中建立一个主评测账号
和一个隔离账号，写入固定语料、执行真实 pgvector 检索，然后在 `finally` 中按账号级联删除
长文本与向量。质量门禁同时检查用例通过率、最终 Top-N 的平均 Recall、Precision、nDCG、MRR 和禁止材料命中率，JSON
报告保存在 `data/eval-reports/`，其中不保存连接串、API Key 或临时账号 ID。

默认 `configured` 模式读取 `.env` 中的 Embedding 和 Rerank 配置，适合形成上线结论；
`-EmbeddingMode local_hash` 是完全本地、确定性的管线冒烟测试，只能证明建库、检索、指标和
清理链路可运行，不能用于判断中文语义召回准确率。更换 Embedding 模型、Rerank 模型、切片
规则或检索参数后必须重新执行并保存报告。门禁失败时应查看失败用例和标签分组，不得通过
降低阈值掩盖具体行业、数值或冲突材料的退化。

首版 `visual-summary` 用例验证的是项目扫描阶段由多模态模型生成的视觉文字摘要能否被文字
RAG 召回，不等于直接检索原始图片像素。上线前仍要逐步加入经过授权和脱敏的真实 PDF、
表格、设计图与难负样本，并单独评估图像向量或 CAD 解析能力。

### GitHub 真实文件端到端门禁

版本化真实文件评测分成两个用途不同的套件：

- `evals/rag/github_artifact_suite.json`：12 份材料的冒烟集，只验证端到端链路。
- `evals/rag/github_hard_negative_suite.json`：33 份材料、33 条问题的正式发布集，包含 31 条困难
  负样本约束和 12 条保留问题，`Top 5` 只占语料 15.2%。

材料覆盖工业图纸、IFC BIM、施工进度、医疗、财务、视觉设计和物流。来源仓库分别采用 MIT、
Apache-2.0 或 CC-BY-4.0 许可证；清单必须同时记录固定提交、文件大小和 SHA-256，禁止使用
`main`、`master` 或任意外部下载地址。冒烟集执行：

```powershell
.\scripts\validate_rag_artifacts.ps1 `
  -Python E:\Anaconda\python.exe `
  -DatabaseUrl "postgresql+psycopg://job_agent@127.0.0.1:5432/job_agent"
```

正式发布集执行：

```powershell
.\scripts\validate_rag_artifacts.ps1 `
  -BenchmarkRole release `
  -Python E:\Anaconda\python.exe `
  -DatabaseUrl "postgresql+psycopg://job_agent@127.0.0.1:5432/job_agent"
```

评估 K/N 候选参数时增加：

```powershell
.\scripts\validate_rag_artifacts.ps1 `
  -BenchmarkRole release `
  -Python E:\Anaconda\python.exe `
  -DatabaseUrl "postgresql+psycopg://job_agent@127.0.0.1:5432/job_agent" `
  -TuneParameters `
  -TuneKValues "10,15,20,30" `
  -TuneNValues "3,5" `
  -TuningRepetitions 3
```

扫描过程只解析和索引一次材料，多轮按参数组合交错运行；只用 `development` 比较各组合相对
当前线上基线的质量与核心召回/重排 P95，选型完成后才运行 `holdout`。2026-08-29 的三轮评测在质量不变的前提下将默认值从 `K=20/N=5` 调整为 `K=10/N=5`；后续每次换模型、切片规则或语料分布后都必须重新校准。报告另外统计视觉原图复查和
端到端平均/P95。候选在留出集的质量门禁或端到端性能防回退门禁失败时，不能
替换线上默认值。远程视觉复查可能产生长尾，正式变更至少需要多轮结果一致，不能根据一次最快值调参。

下载器只根据清单生成 `raw.githubusercontent.com` 地址，并限制重定向主机、单文件大小和总
下载量。全部下载内容在内存中复核大小与 SHA-256，通过后才进入现有项目采集入口；该入口再
执行清单规划、敏感路径拒绝、本地 EICAR 检查、格式解析、OCR/多模态提取和视觉副本保存。
ClamAV 自身由 `validate_file_scanning.ps1` 单独验收，避免 RAG 质量测试因病毒库服务状态产生
无关波动。

脚本使用临时账号和本地临时对象目录。视觉模型调用通过临时模拟余额完成，调用仍会产生模型
供应商费用；完成或异常时均按账号级联删除余额流水、长文本和向量，并删除临时视觉副本。报告
只包含来源 ID、行业、提取方法、字符数、视觉状态和检索指标，不包含原始文件、连接串、密钥或
临时账号 ID。第三方原文件不得提交到仓库。

`-EmbeddingMode local_hash -VisualMode disabled` 只用于离线检查清单、提取、建库、检索和清理
程序。发布结论必须使用默认 `configured` 模式，并在更换视觉模型、Embedding、Rerank、OCR、
切片规则或项目解析器后重新运行。若上游固定文件不可用或摘要变化，应先人工核对许可证与内容，
再显式更新提交、大小和 SHA-256，不能跳过完整性校验。

正式报告不能只看 `Recall@5`。必须同时审查 `Recall@1/3/5`、Precision@K、nDCG@K、MRR、
困难负样本命中率和行业分组。`development` 可用于日常调参；`holdout` 只在阶段验收时运行和
查看，不能针对其中某条问题反复改查询措辞或预期来源。正式门禁的阈值是首版工程基线，不是
行业通用标准；积累真实用户材料后应重新校准，但不得为了通过一次失败而直接降低阈值。

宿主机脚本不能使用 Compose 网络内的 `postgres` 主机名。若 `.env` 没有宿主机
`JOB_AGENT_DATABASE_URL`，必须通过 `-DatabaseUrl` 传入 `127.0.0.1` 地址；启用数据库密码后，
应从当前终端环境或受控密钥文件传入，不要把密码写入 README、脚本或 Git。

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
