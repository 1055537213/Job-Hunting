# 求职助手 Agent

[![CI](https://github.com/1055537213/Job-Hunting/actions/workflows/ci.yml/badge.svg)](https://github.com/1055537213/Job-Hunting/actions/workflows/ci.yml)

一个面向求职准备场景的多账号 Agent 工作台。系统把候选人档案、职位、项目证据、简历文件和对话记忆组织成可追溯的数据链路，再通过 LangChain Agent、RAG 和 Celery 后台任务完成资料整理、职位匹配和定制简历生成。

系统只处理用户主动提供或授权的内容，不登录招聘平台、不抓取隐藏接口、不自动投递，也不自动向招聘方发送消息。

## 1. 项目简介

### 1.1 项目是什么

求职助手 Agent 是一个带有账号隔离、候选人档案、职位导入、项目证据分析、RAG 检索、简历生成、余额计费和管理后台的模块化单体应用。

前端使用同一套静态资源按路径切换认证页、工作台、个人中心和管理员后台；后端由 FastAPI 提供页面、API 和 SSE 流式对话接口。

### 1.2 项目解决什么问题

求职者的资料通常分散在简历、职位描述、项目代码、工业 PDF、图片和聊天记录中。直接让模型自由生成，容易出现事实污染、重复记录和无法追溯的问题。本项目重点解决：

- 以结构化档案保存学历、经历、技能熟练度、求职方向、城市和薪资偏好。
- 通过职位文本或截图导入可比较的职位信息。
- 分析用户主动提供的公开 GitHub 项目，并先生成待确认的项目经历卡片。
- 处理代码、PDF、图片、表格、DOCX 等多种项目材料，保留来源定位和证据关系。
- 把确认后的事实和长文本建立账号隔离的 pgvector 索引，支持可追溯检索。
- 使用 Agent 对话完成档案维护、职位匹配、材料整理和简历生成。
- 记录 Token 用量、余额流水、工具轨迹、后台任务和管理员审计事件。

数据边界如下：PostgreSQL 是结构化事实和任务状态的权威来源；pgvector 是可重建的派生索引；MinIO/S3 保存二进制对象；Redis 负责 Celery 消息、共享限流和并发租约。

## 2. 在线演示 / 效果截图

当前没有公开演示站点，也没有把用户数据或未脱敏运行截图提交到仓库。启动本地环境后可访问：

| 页面或服务 | 地址 | 用途 |
| --- | --- | --- |
| 认证页 | `http://127.0.0.1:8000/login` | 登录、注册、邮箱验证和密码找回 |
| 工作台 | `http://127.0.0.1:8000/` | 档案、职位、项目、对话和简历 |
| 个人中心 | `http://127.0.0.1:8000/profile` | 余额、模拟充值、消费流水和账号安全 |
| 管理后台 | `http://127.0.0.1:8000/admin` | 用量与账号、请求观测、审计和工具轨迹 |
| FastAPI 文档 | `http://127.0.0.1:8000/docs` | Swagger API 文档 |
| ReDoc | `http://127.0.0.1:8000/redoc` | 只读接口文档 |
| 健康检查 | `http://127.0.0.1:8000/api/health` | 检查数据库、队列、对象存储和模型摘要 |

同一服务器已有其他项目占用 `80/443` 时，生产共存拓扑默认使用 `https://<公网IP>:8443`，不会占用旧项目的默认端口。

如需补充截图，只能提交脱敏后的界面截图；不要提交 `.env`、用户简历、数据库导出、对象存储原件或评测运行数据。

## 3. 功能清单

### 账号与安全

- 多账号隔离、Session Cookie 和资源归属校验。
- 注册、登录、邮箱验证、密码重置、修改密码和账号注销。
- Argon2id 密码哈希、CSRF、防重放、请求限流、安全响应头和请求 ID。
- 账号数据导出、全部设备退出和账号删除后的关联数据清理。
- 管理员启用/停用账号，并保留管理员审计事件。

### 候选人档案与 Agent

- 管理教育经历、工作经历、项目经历、技能熟练度、目标方向和城市偏好。
- 通过对话补充或修改档案，支持技能同义词和大小写规范化去重。
- 对话历史保存、会话恢复、历史压缩和 SSE 流式回复。
- 轻量意图路由只处理高置信度低风险请求；修改、确认、指代、多步骤和异常场景回退主 Agent。
- 工具调用按任务记录，前端只展示用户需要的状态，不展示内部工具名、数据库 ID 或字段名。

### 职位与匹配

- 粘贴职位文本或上传职位截图进行导入。
- 规则解析职位字段，并可用模型辅助分类技能要求。
- 账号范围内的重复职位检测和删除清理。
- 根据学历、经验、技能、城市、薪资和硬性限制计算可解释匹配结果。

### 项目证据与知识库

- 公开 GitHub 仓库只读分析，固定提交和哈希后再处理。
- 本地项目采用“清单/哈希预扫描 + 按需分批传输”，避免一次性上传整个目录。
- 对代码、文本、PDF、图片、CSV/XLSX、DOCX/PPTX 等材料分流提取。
- 项目分析结果先生成待确认卡片；用户确认后才进入候选人事实和 RAG。
- 文本 Chunk 按标题、段落、列表、代码块、表格和 PDF 页边界组织，过长内容才使用句子和长度上限兜底切分。
- 文字向量与视觉向量混合召回；查询中的否定条件、数值和多步骤意图会在检索后进行规则处理。
- 删除项目时同步清理项目卡片、长文本、视觉知识项、对象存储对象和向量索引。

### 简历与后台任务

- 导入 DOCX、文字 PDF 和扫描 PDF 简历。
- OCR、GitHub 分析、项目归档分析、RAG 索引和职位定制简历由 Worker 执行。
- 后台任务具备幂等键、原子认领、进度、有限重试、错误摘要和失联回收。
- 生成的 DOCX/PDF 作为独立版本保存，不覆盖原始简历。

### 计费与管理

- 按模型实际 Token 用量记录消费并从余额扣减。
- 个人中心提供本地模拟充值；生产环境关闭模拟充值，真实支付仍是后续工作。
- 管理员可以为指定账号人工补款，并记录原因和审计事件。
- 管理后台显示余额、Token 明细、工具调用、请求观测、账号邮件投递和管理员审计。
- Token 明细、工具调用和余额流水都采用分页查询，按保留规则清理过期记录。

### 运维与可观测性

- Prometheus 请求指标和五类基础告警：Web 不可用、5xx 比例过高、响应过慢、安全拦截突增、高并发。
- Alertmanager 聚合告警并通过 SMTP 发送邮件。
- Loki + Alloy 集中收集 Docker 日志，Tempo + OpenTelemetry 保存 Trace，Grafana 统一查看。
- PostgreSQL/MinIO 备份恢复、Worker 故障恢复、ClamAV 文件扫描和安全扫描验收脚本。

## 4. 技术栈

### 后端

- Python 3.12.13
- FastAPI + Uvicorn
- LangChain Agent + LangGraph Checkpointer
- Celery + Redis
- SQLAlchemy + Alembic
- PostgreSQL 16 + pgvector

### 前端

- Vue 3 Global Build，运行时文件随仓库提供
- 原生 HTML、CSS 和 JavaScript
- SSE 流式对话
- 路径视图：`/login`、`/`、`/profile`、`/admin`

### 数据库与基础设施

- PostgreSQL：账号、档案、职位、项目、长文本、向量、任务、账务、审计和用量事实。
- MinIO/S3-compatible：简历原件、导出文件、项目原件和视觉派生对象。
- Redis：Celery Broker、共享限流、并发租约和短期运行状态。
- ClamAV：生产上传文件和项目归档的病毒扫描。
- Prometheus：指标采集、查询和告警规则。
- Alertmanager：告警聚合、去重、恢复通知和 SMTP 投递。
- Loki、Alloy、Tempo、OpenTelemetry、Grafana：日志、Trace 和可视化排障。
- Caddy：独占服务器生产 HTTPS；Nginx：共享服务器 `8443` IP HTTPS 共存入口。
- Docker Compose：开发、生产、共存、恢复、负载和观测验收拓扑。

### 质量与安全

- Pytest、Ruff、`compileall`、Node 前端回归测试。
- pip-audit、Trivy 和 CycloneDX SBOM。
- CI 固定运行时版本、锁定依赖和关键容器镜像摘要。

## 5. 项目亮点

1. **事实与推断分离**：模型只能提出项目摘要或表达方式，结构化事实必须经过工具和用户确认。
2. **统一工具边界**：ToolRegistry 是 Agent、直达路由、审计和协议适配器共享的唯一工具目录，避免多套实现漂移。
3. **多租户隔离贯穿全链路**：数据库、对象键、RAG 查询、后台任务、余额和审计事件都校验账号归属。
4. **任务可恢复且可幂等**：Redis 只传递受控任务键，PostgreSQL 保存任务事实，避免重复执行、重复扣费和重复导出。
5. **多模态证据链**：职位截图、项目图片和复杂 PDF 页面可经过 OCR、视觉分析和视觉向量检索，并保留来源定位。
6. **检索漏斗可调优**：Retriever 先取 Top-K 候选，再由 Reranker 取最终 Top-N；当前线上默认值为 `K=10`、`N=5`。
7. **生产发布可回退**：CI 成功后才发布不可变 GHCR 镜像；生产部署要求完整提交 SHA、人工确认、迁移前备份和健康检查。
8. **可观测且低敏**：日志和 Trace 共享 `trace_id`，但不采集请求正文、Cookie、API Key、模型提示词或用户文件原文。

## 6. 目录结构说明

```text
.
├─ src/job_hunting_agent/
│  ├─ web.py                  # FastAPI 页面、API、SSE、鉴权和管理接口
│  ├─ agent.py                # LangChain Agent、系统提示词、记忆和路由编排
│  ├─ app.py                  # 业务服务门面和模块编排
│  ├─ tool_registry.py        # 工具定义、校验、执行和统一结果契约
│  ├─ job_hunting_tools.py    # 求职领域工具的唯一注册位置
│  ├─ langchain_tool_adapter.py # ToolRegistry 到 LangChain 的适配器
│  ├─ mcp_tool_adapter.py     # MCP 兼容结构适配器，不启动 MCP Server
│  ├─ models.py               # 领域记录、输入模型和结果模型
│  ├─ storage.py              # 数据库无关的仓储和事务逻辑
│  ├─ sqlalchemy_store.py     # PostgreSQL 仓储实现
│  ├─ database_schema.py      # SQLAlchemy 表、约束和索引
│  ├─ config.py               # `.env` 和运行时配置
│  ├─ model_gateway.py        # 模型调用、usage、重试、并发和计费边界
│  ├─ rag.py                  # Embedding、Rerank、切片和检索协议
│  ├─ pgvector_rag.py         # 文本 RAG 索引、检索和删除
│  ├─ pgvector_visual.py      # 视觉知识项向量写入和校验
│  ├─ project_*.py             # GitHub、本地项目和项目证据处理
│  ├─ resume_*.py              # 简历解析、写作和 DOCX/PDF 导出
│  ├─ task_registry.py         # 后台任务定义和执行注册表
│  ├─ background_tasks.py      # Celery 任务、重试和失联回收
│  ├─ worker.py / beat.py      # Worker 和定时任务入口
│  ├─ account_*.py / auth.py   # 账号生命周期、邮件 Outbox 和认证
│  ├─ observability*.py        # 日志、Trace、指标和告警配置
│  ├─ file_scanning.py         # 本地扫描和 ClamAV 边界
│  └─ web_static/              # 前端页面、脚本、样式和 Vue 运行时
├─ alembic/                    # Alembic 数据库迁移
├─ tests/                      # Python 测试和前端回归测试
├─ evals/rag/                  # RAG 黄金集、困难负样本和真实文件评测清单
├─ scripts/                    # 备份、恢复、负载、安全和企业验收脚本
├─ deploy/                     # Caddy、Nginx、Prometheus、观测和生产模板
├─ docs/                       # ADR、架构决策、运行和发布文档
├─ compose.yaml                # 基础开发拓扑
├─ compose.dev.yaml            # 源码挂载和热更新覆盖
├─ compose.prod.yaml           # 单机生产覆盖
├─ compose.coexist.yaml        # 同机轻量共存覆盖
├─ compose.*-test.yaml         # 恢复、扫描、观测、扩容和验收覆盖
├─ Dockerfile                  # Web/Worker/Beat/Migrate 共用镜像
├─ pyproject.toml              # 包元数据、入口命令和测试配置
├─ requirements.lock           # 运行时锁定依赖
└─ .env.example                # 脱敏配置模板
```

## 7. 运行步骤

### 7.1 环境要求

- Docker Desktop 或 Docker Engine + Compose v2
- Git
- Python 3.12（宿主机测试）
- Node.js 22（前端回归测试）
- 一个 OpenAI-compatible Chat、Embedding 和可选 Rerank/视觉模型配置

### 7.2 获取代码并配置环境变量

```powershell
git clone https://github.com/1055537213/Job-Hunting.git
Set-Location Job-Hunting
Copy-Item .env.example .env
```

本地至少填写模型和运行时密码：

```dotenv
JOB_AGENT_LLM_PROVIDER=your-chat-provider
JOB_AGENT_LLM_MODEL=your-chat-model
JOB_AGENT_LLM_API_KEY=your-api-key
JOB_AGENT_LLM_BASE_URL=https://api.example.com/v1
JOB_AGENT_OBJECT_STORAGE_ACCESS_KEY=your-minio-access-key
JOB_AGENT_OBJECT_STORAGE_SECRET_KEY=your-minio-secret
JOB_AGENT_REDIS_PASSWORD=your-strong-redis-password
```

本地开发默认使用回环地址、控制台邮件、本地文件扫描和开发数据库认证。生产配置请以 `deploy/env.production.example` 为模板，必须使用独立密钥、密码认证 PostgreSQL、Redis、ClamAV、SMTP 和 HTTPS。

不要把 `.env`、服务器密钥、真实邮箱密码、模型 API Key 或生产备份提交到 Git。

### 7.3 启动开发环境

```powershell
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
docker compose -f compose.yaml -f compose.dev.yaml ps
```

访问 `http://127.0.0.1:8000/`。查看日志：

```powershell
docker compose -f compose.yaml -f compose.dev.yaml logs --tail 100 web worker beat
```

修改前端后，开发覆盖会挂载 `src/` 并启用 Uvicorn reload；浏览器仍显示旧资源时执行硬刷新。停止服务但保留数据卷：

```powershell
docker compose -f compose.yaml -f compose.dev.yaml down
```

只有确认要删除本地 PostgreSQL、MinIO、Redis 和 Prometheus 数据时，才执行 `down -v`。

### 7.4 本地质量检查

```powershell
ruff check src tests alembic
python -m compileall -q src tests alembic
python -m pytest -q
Get-ChildItem tests -Filter 'frontend_*.mjs' | ForEach-Object { node $_.FullName }
```

Compose 配置检查：

```powershell
$env:JOB_AGENT_REDIS_PASSWORD='ci-redis-password'
docker compose --env-file .env.example -f compose.yaml config --quiet
docker compose --env-file .env.example -f compose.yaml -f compose.prod.yaml config --quiet
docker compose --env-file .env.example -f compose.yaml -f compose.prod.yaml -f compose.coexist.yaml config --quiet
```

专项验收入口：

```powershell
.\scripts\validate_local_release.ps1
.\scripts\validate_backup_restore.ps1
.\scripts\validate_file_scanning.ps1
.\scripts\validate_multi_replica.ps1
.\scripts\validate_worker_recovery.ps1
.\scripts\validate_rag_retrieval.ps1
.\scripts\validate_rag_artifacts.ps1
.\scripts\validate_e2e_load.ps1 -Profile smoke
.\scripts\security_scan.ps1
```

RAG 正式发布集必须在上线前使用真实 Embedding、视觉模型和 Reranker 重新测量。`local_hash` 只用于验证评测管线，不代表语义召回质量。

### 7.5 生产部署

生产部署使用通过 CI 的不可变镜像，不在服务器上直接从源码临时构建。完整生产拓扑使用 `compose.prod.yaml`，同机已有其他项目占用 `80/443` 时使用共存拓扑：

```bash
docker compose --env-file /opt/job-hunting-agent/shared/.env \
  -f /opt/job-hunting-agent/current/compose.yaml \
  -f /opt/job-hunting-agent/current/compose.prod.yaml \
  -f /opt/job-hunting-agent/current/compose.coexist.yaml \
  config --quiet
```

共存拓扑的公网入口是 `https://<公网IP>:8443`，应用 Web 只绑定服务器回环地址 `127.0.0.1:18081`。阿里云安全组只开放业务需要的 `8443` 和证书 HTTP-01 续期需要的 `80`，不要开放 `18081`、Prometheus 或 Alertmanager 端口。

仓库已经接入受控 CD：

1. 推送到 `master` 后，`CI` 执行 Python、前端、Compose、配置和安全检查。
2. CI 成功后，`Publish release image` 重新构建并扫描精确提交，发布 `ghcr.io/<owner>/<repo>:sha-<提交前12位>`。
3. 管理员手动启动 `Deploy production`，填写完整 40 位提交 SHA、确认词 `DEPLOY`，并选择 `coexist` 或 `standalone`。
4. GitHub `production` Environment 审批通过后，工作流通过固定 SSH 指纹上传部署包和镜像，在服务器上执行迁移、备份、健康检查和失败回滚。

服务器上的生产 `.env` 保留在 `shared/.env`，不由 GitHub Actions 上传。首次部署前必须准备对象存储 bucket、生产密钥、证书和 GitHub Actions 所需的 SSH/Environment 配置。详细步骤见 [生产发布与恢复基线](docs/learning/production-release.md)。

## 8. 接口文档

启动 Web 后，以 `/docs` 和 `/redoc` 生成的接口为准。主要接口如下：

| 模块 | 方法与路径 | 说明 |
| --- | --- | --- |
| 认证 | `POST /api/auth/register` | 注册账号 |
| 认证 | `POST /api/auth/login` | 登录并设置 Session |
| 认证 | `POST /api/auth/verify-email` | 消费邮箱验证令牌 |
| 认证 | `POST /api/auth/password-reset/request` | 请求密码重置 |
| 账号 | `GET /api/account/export` | 导出本人数据 |
| 账号 | `POST /api/account/delete` | 删除或匿名化账号数据 |
| 档案 | `GET/POST /api/profiles` | 列出或创建候选人档案 |
| 对话 | `POST /api/chat/stream` | SSE Agent 对话 |
| 职位 | `POST /api/jobs` | 导入职位文本 |
| 职位 | `POST /api/jobs/screenshots` | 导入职位截图 |
| 匹配 | `GET /api/matches/{candidate_id}` | 返回职位匹配结果 |
| 项目 | `POST /api/projects/github` | 提交公开 GitHub 项目分析 |
| 项目 | `POST /api/projects/local/manifest` | 提交本地项目文件清单 |
| 项目 | `POST /api/projects/{record_id}/confirm` | 确认项目经历卡片 |
| 简历 | `POST /api/resumes/upload` | 上传原始简历 |
| 简历 | `POST /api/resumes/{artifact_id}/tailor` | 提交职位定制简历任务 |
| 任务 | `GET /api/tasks/{task_key}` | 查询后台任务状态 |
| RAG | `GET /api/rag/search` | 账号隔离的检索接口 |
| 运维 | `GET /api/health` | 健康检查 |
| 运维 | `GET /internal/metrics` | Prometheus 指标，仅供内网抓取 |

已登录的写操作需要同源 Cookie 和 `X-CSRF-Token`；管理接口要求管理员角色；所有资源接口都会校验账号归属。`/internal/metrics` 不应通过公网反向代理暴露。

## 9. 常见问题（FAQ）

### 为什么访问 `localhost:8000` 失败？

部分 Windows 环境会把 `localhost` 解析到 IPv6 `::1`，而服务只监听 IPv4。请使用 `http://127.0.0.1:8000`。

### 修改前端后为什么没有变化？

开发时必须使用 `compose.dev.yaml`，它会挂载源码并启用 reload；然后执行浏览器硬刷新。生产镜像中的前端修改则需要重新提交、通过 CI、发布镜像并部署新版本。

### 为什么登录或写操作返回 403？

可能是邮箱验证未完成、协议版本不一致、Session 失效或 CSRF Token 缺失。先重新加载页面获取当前 CSRF Cookie，再重试写操作。

### 为什么模型请求失败或提示余额不足？

检查模型 provider、模型名、API Key、Base URL 和账号余额。余额不足时统一提示“余额不足，请先充值后重试。”；模型超时、限流和熔断错误属于不同的运行状态。

### 为什么职位或项目没有立即完成？

OCR、GitHub 分析、项目归档、RAG 索引和简历导出在队列开启时由 Worker 执行。检查 `worker`、`beat`、Redis，并通过 `GET /api/tasks/{task_key}` 查看任务状态和错误摘要。

### 删除项目后为什么不能马上重新导入？

删除会清理数据库、长文本、视觉派生对象、对象存储和向量索引；若此前任务仍在运行，清理可能需要等待任务结束。重新导入时必须使用新的任务状态，不能复用已取消任务的临时文件。

### 当前 RAG 的 Top-K 和 Top-N 是多少？

默认 Retriever Top-K 为 `10`，Reranker 最终 Top-N 为 `5`。流程是“全量知识库 -> Retriever 候选 -> 完整精度 Top-K -> Reranker Top-N -> Agent”。K/N 只能依据 development 集的召回质量、困难负样本和 P95 延迟调优，不能根据 holdout 问题逐条修改。

### 项目是否运行 MCP Server 或自动投递？

当前没有运行 MCP Server。`mcp_tool_adapter.py` 只提供兼容结构；内部工具由 ToolRegistry 和 LangChain Agent 使用。系统也不会自动登录招聘平台、自动投递或自动联系 HR。

### 生产共存部署如何访问？

使用 `https://<公网IP>:8443`。`18081` 只供服务器本机回环检查，Prometheus 和 Alertmanager 只绑定回环地址。旧项目继续使用原来的 `80/443`，两套项目通过端口和独立 Compose 项目隔离。

## 10. TODO / 未来计划

- [ ] 接入真实支付渠道、签名 Webhook、退款状态机和渠道对账。
- [x] 建立跨行业 RAG 黄金集、困难负样本和真实文件评测集。
- [x] 建立 Retriever Top-K / Reranker Top-N 参数扫描、P95 统计和质量防回退门禁。
- [ ] 持续扩充经过授权的真实行业语料，并按正式模型重新校准阈值。
- [x] 接入 Prometheus、Alertmanager、OpenTelemetry、Loki、Tempo 和 Grafana。
- [x] 建立备份恢复、Worker 恢复、ClamAV 和告警投递验收脚本。
- [ ] 在出现明确外部调用方后，为 MCP adapter 增加鉴权、授权和 Server 生命周期。
- [ ] 建立更严格的类型检查基线并消化类型债务。
- [ ] 完成生产服务器的渗透测试、灾难恢复演练、密钥轮换和容量基线。
- [ ] 增加工业 PDF 表格/图注坐标、父子 Chunk、数值范围检索和 CAD 解析能力。
- [ ] 建立公开线上演示环境和脱敏效果截图。

## 11. 联系方式 / 声明

- GitHub 仓库：<https://github.com/1055537213/Job-Hunting>
- Issue 反馈：<https://github.com/1055537213/Job-Hunting/issues>
- 架构边界：[CONTEXT.md](CONTEXT.md)
- 决策记录：[DECISION_MAP.md](DECISION_MAP.md)
- ADR：[docs/adr/](docs/adr/)
- 发布与恢复：[docs/learning/production-release.md](docs/learning/production-release.md)

本项目仅提供求职准备、信息整理和材料生成辅助，不保证职位匹配结果、面试结果或录用结果。候选人应对提交给招聘平台的内容、真实性、隐私授权和最终发送行为负责。
