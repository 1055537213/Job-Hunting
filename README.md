# Job-Hunting Agent Workbench

[![CI](https://github.com/1055537213/Job-Hunting/actions/workflows/ci.yml/badge.svg)](https://github.com/1055537213/Job-Hunting/actions/workflows/ci.yml)

一个面向求职准备场景的多账号 AI 工作台。系统把候选人档案、职位、项目证据、简历材料和对话记忆组织成可追溯的数据链路，再通过 Agent、RAG 和后台任务完成资料整理、职位匹配与简历生成。

系统只处理候选人主动提供或授权的内容，不登录招聘平台、不抓取隐藏接口、不自动投递，也不自动向招聘方发送消息。

## 1. 项目简介

### 项目是什么

这是一个 Python + Java 的企业化演进项目：

- Python 负责 FastAPI 外部 Web API、Agent、RAG、文件分析、简历生成和异步 AI 任务。
- Java Spring Boot 平台服务分阶段接管账号、Session、权限和账务等平台能力。
- 前端仍由 Python Web 提供，外部 URL 和接口保持兼容，迁移通过环境开关控制。
- master 保留纯 Python 版本；java-platform-migration 是独立的 Python+Java 版本。

### 解决的问题

求职资料往往分散在简历、职位描述、项目代码、PDF、图片和聊天记录中。项目通过结构化事实、来源定位、用户确认和账号隔离，减少自动生成中的事实污染与不可追溯问题。

## 2. 在线演示 / 效果截图

当前没有公开演示站点，也不提交用户数据或未脱敏运行截图。启动本地环境后可访问：

| 页面 | 地址 | 内容 |
| --- | --- | --- |
| 登录/注册 | http://127.0.0.1:8000/login | 登录、注册、邮箱验证、邮箱变更和密码找回 |
| 工作台 | http://127.0.0.1:8000/ | 档案、职位、项目、对话和简历 |
| 个人中心 | http://127.0.0.1:8000/profile | 余额、演示充值、消费流水和账号安全 |
| 管理后台 | http://127.0.0.1:8000/admin | 账号、用量、请求观测、审计和工具轨迹 |
| API 文档 | http://127.0.0.1:8000/docs | Swagger 文档 |
| 健康检查 | http://127.0.0.1:8000/api/health | 数据库、队列、对象存储和模型状态 |

生产共存拓扑使用 https://<公网IP>:8443，不占用同机旧项目的 80/443。真实演示地址、截图和运行数据应在脱敏并获得授权后再补充。

## 3. 功能清单

### 账号与安全

- 多账号隔离、HttpOnly Session Cookie、CSRF、防重放、限流和安全响应头。
- 注册、登录、邮箱验证、密码重置、修改密码、全部设备退出和账号注销。
- 登录后可发起邮箱变更；新邮箱确认成功后会撤销旧登录会话，并要求重新登录。
- Argon2id 密码哈希，Java 兼容现有 Python 哈希格式。
- 账号导出、可恢复删除任务和管理员启用/停用账号。

### 候选人档案与 Agent

- 管理学历、工作经历、技能熟练度、求职方向、城市偏好和薪资要求。
- 通过对话修改档案，技能支持大小写和同义词规范化去重。
- 对话历史、摘要记忆、SSE 流式回复和会话恢复。
- 高风险、多步骤和不确定请求回退到主 Agent，并保留工具审计。

### 职位与匹配

- 粘贴职位文本或上传职位截图。
- 解析职位名称、城市、薪资、经验、学历、公司和技能要求。
- 按账号检测重复职位，并支持用户删除职位。
- 根据学历、经验、技能、城市、薪资和硬性限制生成可解释匹配结果。

### 项目证据与知识库

- 只读分析公开 GitHub 项目，固定提交和哈希后再处理。
- 本地项目采用文件清单/哈希预扫描和按需分批传输。
- 分流处理代码、文本、PDF、图片、表格、DOCX 和 PPTX。
- 分析结果先生成待确认项目卡片，用户确认后才写入候选人事实和 RAG。
- 文本向量、视觉向量、关键词、数值和否定条件共同参与检索后处理。
- 删除项目时清理数据库、长文本、视觉项、对象存储对象和向量索引。

### 简历与后台任务

- 导入 DOCX、文字 PDF 和扫描 PDF 简历。
- OCR、项目分析、RAG 索引和简历导出由 Worker 执行。
- 任务具备幂等键、原子认领、进度、有限重试、错误摘要和失联回收。
- 管理员可以查看失败任务并重新投递，重试动作写入审计。

### 计费与管理

- 按实际 Token 用量记录消费并从余额扣减。
- 个人中心支持受限的演示充值；真实支付、签名 Webhook、退款和对账暂未接入。
- 管理员可以为账号人工补款，保存原因和审计记录。
- 管理后台展示余额、Token 明细、工具调用、请求观测、邮件投递和管理员审计。
- 余额、Token 和工具记录采用分页查询与保留规则清理。

### 运维与可观测性

- Prometheus 请求指标和 Web 不可用、5xx、慢响应、安全拦截、高并发告警规则。
- Alertmanager、SMTP、Loki、Alloy、Tempo、OpenTelemetry 和 Grafana 配置。
- PostgreSQL/对象存储备份、恢复演练、Worker 恢复和 ClamAV 上传扫描验收脚本。

## 4. 技术栈

### 后端

- Python 3.12、FastAPI、Uvicorn、SQLAlchemy、Alembic。
- LangChain Agent、LangGraph Checkpointer、OpenAI-compatible 模型网关。
- Celery、Redis、PostgreSQL 16、pgvector、MinIO/S3。
- Java 21、Spring Boot 4、Spring JDBC、Maven。

### 前端

- Vue 3 Global Build、原生 HTML/CSS/JavaScript。
- SSE 流式对话。
- 路径视图：/login、/、/profile、/admin。

### 数据库与基础设施

- PostgreSQL：结构化事实、账号、任务、账务、审计、用量和向量。
- Redis：Celery Broker、共享限流、并发租约和 Cache-Aside 业务缓存。
- MinIO/S3：简历、导出文件、项目原件和视觉派生对象。
- ClamAV：上传文件病毒扫描。
- Prometheus/Alertmanager/Grafana/Loki/Tempo：指标、告警、日志、Trace 和可视化。
- Docker Compose：开发、生产、混合、共存、恢复和验收拓扑。
- Caddy 或共存 Nginx：HTTPS 入口。

### 质量与安全

- Pytest、Ruff、compileall、Node 前端回归测试和 Maven 测试。
- pip-audit、OSV Scanner、Trivy 和 CycloneDX SBOM。
- GitHub Actions CI、不可变 GHCR 镜像和人工批准部署工作流。

## 5. 项目亮点

1. **事实与推断分离**：模型生成的项目摘要和简历表达必须经过候选人确认，不能自动伪造经历。
2. **Python + Java 渐进迁移**：Java 迁移不覆盖纯 Python 分支，外部 Python API 契约保持稳定。
3. **账号隔离贯穿全链路**：数据库、对象键、RAG、任务、余额和审计都校验账号归属。
4. **可恢复、可幂等**：任务、充值和扣费使用幂等键，避免重复执行和重复扣费。
5. **多模态证据链**：PDF、图片、表格和项目文件保留来源定位，不只保存孤立文字。
6. **RAG 检索漏斗**：Retriever 先取 Top-K，再由 Reranker 取 Top-N；当前默认 K=10、N=5。
7. **缓存不改变事实**：Redis 只保存可重建结果，故障时回源 PostgreSQL、配置或模型服务。
8. **低敏可观测性**：记录 trace、状态和耗时，不采集 Cookie、API Key、提示词和用户文件原文。
9. **可回滚发布**：CI 通过后发布精确提交镜像，部署前备份，失败时恢复上一组 Python+Java 镜像。

## 6. 目录结构说明

~~~text
.
├─ src/job_hunting_agent/       # Python Web、Agent、RAG、存储、任务和安全模块
│  ├─ web.py                    # FastAPI 页面、API、SSE、认证和管理接口
│  ├─ agent.py                  # Agent、记忆和编排
│  ├─ app.py                    # 业务门面和模块组合
│  ├─ tool_registry.py          # 工具目录、校验、执行和统一结果
│  ├─ storage.py                # 仓储、事务、任务、账务和审计
│  ├─ platform_auth.py          # Python 到 Java 认证边界客户端
│  ├─ platform_billing.py       # Python 到 Java 账务边界客户端
│  ├─ pgvector_rag.py           # 文本检索、重排和删除
│  ├─ pgvector_visual.py        # 视觉知识项向量
│  ├─ project_*.py              # GitHub、本地项目和证据分析
│  ├─ resume_*.py               # 简历解析、写作和导出
│  ├─ background_tasks.py       # Celery 任务、重试和回收
│  └─ web_static/               # Vue 页面、脚本、样式和运行时
├─ platform-service/            # Spring Boot Java 平台服务
│  ├─ src/main/java/            # 账号、Session、账务和系统接口
│  ├─ src/main/resources/       # 配置和内部 OpenAPI 契约
│  └─ Dockerfile                # Java 镜像构建文件
├─ alembic/                     # 数据库迁移
├─ tests/                       # Python、Java 契约和前端回归测试
├─ evals/rag/                   # RAG 黄金集、困难负样本和评测清单
├─ scripts/                    # 本地、生产、恢复、安全和验收脚本
├─ deploy/                     # HTTPS、Prometheus、日志、Trace 和告警配置
├─ docs/                       # ADR、架构、部署、恢复和安全文档
├─ compose.yaml                # 基础开发拓扑
├─ compose.dev.yaml            # 源码挂载和热更新覆盖
├─ compose.platform.yaml       # Java 本地联调覆盖
├─ compose.prod.yaml           # 单机生产覆盖
├─ compose.coexist.yaml        # 同机共存覆盖
├─ compose.hybrid.prod.yaml    # Python+Java 生产覆盖
├─ Dockerfile                  # Python Web/Worker/Beat/Migrate 镜像
├─ platform-service/pom.xml    # Java Maven 依赖和测试配置
└─ .env.example                # 脱敏配置模板
~~~

## 7. 运行步骤

### 环境要求

- Docker Desktop 或 Docker Engine + Compose v2。
- Python 3.12、Node.js 22、JDK 21、Maven 3.9+。
- OpenAI-compatible Chat、Embedding，及可选 Rerank/视觉模型。

### 获取代码和配置

~~~powershell
git clone https://github.com/1055537213/Job-Hunting.git
Set-Location Job-Hunting
git switch java-platform-migration
Copy-Item .env.example .env
~~~

至少填写模型、对象存储、Redis 和平台服务密钥。混合模式还需要：

~~~dotenv
JOB_AGENT_PLATFORM_INTERNAL_TOKEN=至少32字符的随机服务密钥
JOB_AGENT_JAVA_AUTH_ENABLED=true
JOB_AGENT_JAVA_BILLING_ENABLED=false
~~~

不要把 .env、生产密钥、邮箱密码、模型 API Key、数据库导出或备份提交到 Git。

### 运行纯 Python 开发拓扑

先在 .env 中将 JOB_AGENT_JAVA_AUTH_ENABLED 和 JOB_AGENT_JAVA_BILLING_ENABLED 都设为 false；不要在未启动 Java 服务时开启迁移开关。

~~~powershell
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
docker compose -f compose.yaml -f compose.dev.yaml ps
~~~

访问 http://127.0.0.1:8000/。停止服务但保留数据卷：

~~~powershell
docker compose -f compose.yaml -f compose.dev.yaml down
~~~

### 运行 Python + Java 本地拓扑

确保 .env 已填写 JOB_AGENT_PLATFORM_INTERNAL_TOKEN，并将 JOB_AGENT_JAVA_AUTH_ENABLED 设为 true；若同时启用 Java 账务，再将 JOB_AGENT_JAVA_BILLING_ENABLED 设为 true。Compose 会把同一个内部密钥传给两个服务，然后执行：

~~~powershell
docker compose -f compose.yaml -f compose.platform.yaml up -d --build
docker compose -f compose.yaml -f compose.platform.yaml ps
.\scripts\validate_python_java_local.ps1
~~~

联调脚本使用随机 PostgreSQL schema，验证注册、邮箱验证、登录、密码重置、Session 撤销、管理员账号操作、演示充值和扣费，结束后删除临时 schema，不清理现有用户数据。当前 Java+Python 分支只做本地联调，不自动部署服务器。

### 本地质量检查

~~~powershell
ruff check src tests alembic
python -m compileall -q src tests alembic
python -m pytest -q
Get-ChildItem tests -Filter 'frontend_*.mjs' | ForEach-Object { node $_.FullName }
Set-Location platform-service
mvn -B -ntp test
~~~

Compose 配置检查（以下均为占位测试值，不用于生产部署）：

~~~powershell
$env:JOB_AGENT_REDIS_PASSWORD = 'ci-redis-password'
$env:JOB_AGENT_PLATFORM_INTERNAL_TOKEN = 'ci-platform-shared-internal-token'
$env:JOB_AGENT_POSTGRES_PASSWORD = 'ci-postgres-password'
$env:JOB_AGENT_RUNTIME_ENV_FILE = '.env.example'
$env:JOB_AGENT_IMAGE = 'ghcr.io/example/job-hunting-ai:sha-0123456789ab'
$env:JOB_AGENT_PLATFORM_IMAGE = 'ghcr.io/example/job-hunting-platform:sha-0123456789ab'
$env:JOB_AGENT_ACCOUNT_ACTION_SECRET = 'ci-account-action-secret-at-least-32-characters'
$env:JOB_AGENT_DOMAIN = 'agent.example.com'
$env:JOB_AGENT_PUBLIC_BASE_URL = 'https://203.0.113.10:8443'
$env:JOB_AGENT_PUBLIC_IP = '203.0.113.10'
$env:JOB_AGENT_TLS_EMAIL = 'ops@example.com'
$env:JOB_AGENT_GRAFANA_ADMIN_PASSWORD = 'ci-grafana-password'
docker compose --env-file .env.example -f compose.yaml config --quiet
docker compose --env-file .env.example -f compose.yaml -f compose.platform.yaml config --quiet
docker compose --env-file .env.example -f compose.yaml -f compose.prod.yaml -f compose.coexist.yaml -f compose.hybrid.prod.yaml config --quiet
~~~

### CI/CD

推送到 java-platform-migration 后，CI 会运行 Python、前端、Java、PostgreSQL 联调、Compose、安全扫描和容器连接测试。CI 通过后，工作流发布同一提交的：

~~~text
ghcr.io/1055537213/job-hunting-ai:sha-<提交前12位>
ghcr.io/1055537213/job-hunting-platform:sha-<提交前12位>
~~~

本分支当前以本地验收为主；未来部署使用完整提交 SHA、人工批准和 compose.hybrid.prod.yaml。服务器仍需预先准备生产环境变量、证书和部署权限，但无需临时修改应用代码。操作步骤见 [Python + Java 部署指南](docs/learning/python-java-deployment.md)。

## 8. 接口文档

启动 Web 后，以 /docs 和 /redoc 生成的接口为准。常用接口：

| 模块 | 方法与路径 | 说明 |
| --- | --- | --- |
| 认证 | POST /api/auth/register | 注册账号 |
| 认证 | POST /api/auth/login | 登录并设置 Session |
| 认证 | POST /api/auth/verify-email | 消费邮箱验证令牌 |
| 认证 | POST /api/auth/change-email/confirm | 消费邮箱变更令牌并撤销旧会话 |
| 账号 | GET /api/auth/me | 当前账号与余额摘要 |
| 账号 | PATCH /api/account/email | 发起邮箱变更确认邮件 |
| 账号 | GET /api/account/export | 导出本人数据 |
| 档案 | GET/POST /api/profiles | 查询或创建候选人档案 |
| 对话 | POST /api/chat/stream | SSE Agent 对话 |
| 职位 | POST /api/jobs | 导入职位文本 |
| 职位 | POST /api/jobs/screenshots | 导入职位截图 |
| 项目 | POST /api/projects/github | 提交公开 GitHub 项目分析 |
| 项目 | POST /api/projects/local/manifest | 提交本地项目清单 |
| 项目 | POST /api/projects/{record_id}/confirm | 确认项目经历卡片 |
| 简历 | POST /api/resumes/upload | 上传简历 |
| 任务 | GET /api/tasks/{task_key} | 查询后台任务 |
| 计费 | GET /api/me/balance | 查询余额和消费摘要 |
| RAG | GET /api/rag/search | 账号隔离检索 |
| 运维 | GET /api/health | 应用健康检查 |
| 运维 | GET /internal/metrics | Prometheus 内部指标 |

已登录的写操作需要同源 Cookie 和 X-CSRF-Token；管理接口需要管理员角色；资源接口校验账号归属；/internal/metrics 不应暴露到公网。

Java 内部接口不面向浏览器开放，使用 X-Internal-Service-Token 和 X-Trace-Id。账号资料读取契约位于 platform-service/src/main/resources/auth-internal.openapi.yaml 的 POST /internal/v1/auth/accounts/me。

## 9. 常见问题（FAQ）

### 为什么优先保留 Python + Java，而不是直接全量改成 Java？

AI、RAG、OCR 和多模态文件处理仍然依赖 Python 生态；Java 更适合承载账号、权限、账务和稳定平台边界。分阶段迁移可以降低一次性重写风险，同时保持现有前端和业务能力。

### Java 服务是否已经替代 Python 全部代码？

没有。当前已迁移账号注册、Session、邮箱验证、邮箱变更、密码重置、管理员账号操作、当前账号资料读取、显示名称写入，以及可选账务事实源。Python 的 Agent、RAG、文件分析、简历生成、Worker 和外部 Web 入口仍然保留。

### Java 服务不可用时会不会偷偷回退到 Python？

不会。混合认证和 Java 账务路径会失败关闭，返回 503，避免出现双事实源、旧余额或绕过 Java 权限校验。

### RAG 默认 Top-K 和 Top-N 是多少？

当前默认 Retriever Top-K=10，Reranker Top-N=5。先从知识库召回候选，再重排并把最终证据交给 Agent；正式上线前应使用真实发布集、困难负样本和 P95 延迟重新校准。

### 模拟充值是真实支付吗？

不是。它只用于本地演示和测试，当前真实支付、签名 Webhook、退款状态机和渠道对账仍未接入。

### Redis 故障会不会导致业务事实丢失？

不会。Redis 只承担队列、限流、并发租约和可重建缓存；账号、任务、账务和审计事实保存在 PostgreSQL。缓存不可用时应用回源或按失败策略处理。

### 删除项目后为什么可能不能立即重新导入？

项目删除包含数据库、对象存储、长文本和向量索引的清理；如果分析任务仍在运行，后台任务会先完成或被回收。不能复用已取消任务的临时状态。

### 项目会自动登录招聘平台或自动投递吗？

不会。项目只处理候选人主动带回的职位和材料，不运行招聘平台自动化，也不发送未经确认的招聘消息。

## 10. TODO / 未来计划

- [x] 建立 Python + Java 本地联调、内部契约、CI 和独立分支发布流程。
- [x] 迁移 Java 注册、Session、邮箱验证、邮箱变更、密码重置、管理员账号操作、当前账号资料读取、显示名称写入和账号注销闭环。
- [x] 接入 Java 余额查询、演示充值和模型调用扣费的可选路径。
- [x] 接入邮箱变更验证流程；显示名称写入已由 Java 统一负责。
- [ ] 接入真实支付、签名 Webhook、退款状态机和渠道对账。
- [ ] 使用真实行业材料完成正式 RAG 发布集、Top-K/Top-N 和 P95 性能验收。
- [ ] 增加工业 PDF 表格/图注坐标、父子 Chunk、数值范围检索和 CAD 解析能力。
- [ ] 在有明确外部调用方后，为 MCP adapter 增加认证、授权和 Server 生命周期。
- [ ] 持续执行生产恢复演练、密钥轮换、渗透测试和容量基线复测。
- [ ] 根据资源预算决定是否把 Python+Java 分支部署到真实服务器。

## 11. 联系方式 / 声明

- GitHub：<https://github.com/1055537213/Job-Hunting>
- Issues：<https://github.com/1055537213/Job-Hunting/issues>
- 架构边界：[CONTEXT.md](CONTEXT.md)
- 架构决策：[docs/architecture/python-java-boundary.md](docs/architecture/python-java-boundary.md)
- 发布与恢复：[docs/learning/production-release.md](docs/learning/production-release.md)
- Python + Java 联调：[scripts/validate_python_java_local.py](scripts/validate_python_java_local.py)

本项目仅提供求职准备、信息整理和材料生成辅助，不保证职位匹配、面试或录用结果。候选人应对提交给招聘平台的内容、真实性、隐私授权和最终发送行为负责。
