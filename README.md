# 求职助手 Agent

[![CI](https://github.com/1055537213/Job-Hunting/actions/workflows/ci.yml/badge.svg)](https://github.com/1055537213/Job-Hunting/actions/workflows/ci.yml)

一个面向求职准备场景的多账号 Agent 工作台。系统将候选人档案、职位、项目经历、简历文件和对话记忆组织为可追溯事实，并通过 LangChain Agent、RAG 与后台任务完成职位匹配、材料整理和定制简历生成。

## 1. 项目简介

求职过程中，个人信息、职位要求、项目经历和简历版本通常分散在不同位置，直接让模型自由生成又容易混入未经确认的内容。本项目解决以下问题：

- 用结构化档案保存学历、经验、技能熟练度、目标方向、城市和薪资偏好。
- 审核用户粘贴的职位文本或主动上传的职位截图，拒绝无关内容和重复导入。
- 分析用户提供的公开 GitHub 仓库，将推断出的技术与职责先交给用户按组确认。
- 将确认后的长文本建立账号隔离的 pgvector 索引，为匹配和简历生成提供可追溯证据。
- 通过 SSE 流式展示 Agent 回复，通过 Celery Worker 执行 OCR、项目分析和 RAG 索引。
- 记录供应商返回的 Token 用量，按配置价格实时扣减余额，并向管理员提供分页账本和工具调用轨迹。

PostgreSQL 是权威事实源；`rag_chunks` 是可重建的派生索引；Redis 只传递任务键。系统不会自动登录 BOSS、抓取隐藏接口、投递简历或发送招聘消息。

## 2. 页面预览

仓库暂未提供公开在线环境。本地启动后可访问：

| 页面 | 地址 | 用途 |
| --- | --- | --- |
| 登录与注册 | `http://127.0.0.1:8000/login` | 注册、登录和认证反馈 |
| 工作台 | `http://127.0.0.1:8000/workspace` | 档案、Agent 对话、职位、项目和简历 |
| 个人中心 | `http://127.0.0.1:8000/profile` | 余额、消费流水和模拟充值入口 |
| 管理后台 | `http://127.0.0.1:8000/admin` | 账号用量、余额账本、工具轨迹、请求观测和管理员审计 |
| Swagger | `http://127.0.0.1:8000/docs` | 交互式 API 文档 |

前端是随 FastAPI 一起发布的 Vue 单页应用，不需要单独启动 Node 开发服务器。

## 3. 功能清单

### 账号与工作区

- 邮箱注册、Argon2id 密码哈希、服务端 Session、CSRF 和登录限流。
- 登录页、工作台、个人中心和管理后台使用独立路由。
- 一个账号可创建多个候选人档案，每个档案可维护多个独立对话。
- 账号、档案、职位、项目、简历、RAG 和用量数据均按归属隔离。

### 候选人档案与对话

- 保存学历、经验、技能熟练度、证书、求职方向、城市和薪资偏好。
- 技能名称支持大小写与常见别名归一化，明确不会的技能不进入匹配和简历证据。
- 对话可更新档案事实；明确的“改为”语义会替换目标方向而不是追加。
- SSE 流式回复、持久化聊天记录、上下文恢复和超预算历史压缩。
- 任务过程以内嵌折叠区显示，任务完成后自动收起。

### 职位、项目与简历

- 粘贴职位文本导入，或上传职位截图后由多模态模型识别并由本地解析器复审。
- 职位、候选人、项目和原始简历按各自归属范围进行内容指纹去重。
- 按学历、经验和明确禁忌做硬筛选，按技能、方向、城市和薪资偏好排序并解释。
- 分析公开 GitHub 仓库，不执行仓库代码，不读取敏感文件；推断内容必须由用户按组确认。
- 删除项目时同步删除 PostgreSQL 卡片、长文本事实和对应 pgvector 索引。
- 上传 DOCX、文字 PDF 或扫描 PDF；扫描件由 Worker OCR。
- 基于职位和已确认候选人证据生成独立 DOCX/PDF 定制简历，不覆盖原始简历。

### RAG、后台任务与管理

- 结构化事实与长文本索引分离，Embedding 身份变化时不会混用旧向量。
- 检索先做账号与候选人过滤，再进行余弦相似度召回，可选 Rerank。
- Celery 任务支持幂等键、原子认领、进度、有限重试、错误摘要和刷新后恢复。
- 管理后台展示账号余额、消费流水、Token 明细、工具调用流程、请求指标和管理员审计。
- Token、工具调用和余额流水每账号最多保留 `5` 页，每页 `100` 条，超出 `500` 条的旧记录从数据库删除。
- 新账号余额默认为 `0`；开发环境可模拟充值，生产环境在接入真实支付前禁用该入口。

## 4. 技术栈

| 层次 | 技术 | 主要职责 |
| --- | --- | --- |
| 前端 | Vue 3、HTML、CSS、JavaScript | 多路由工作台、折叠任务过程、管理后台和响应式交互 |
| 通信 | FastAPI、SSE、JSON API | 认证 API、业务接口、文件下载和流式回复 |
| Agent | LangChain、LangGraph、OpenAI-compatible API | 模型适配、工具调用、会话状态和流式 Agent |
| 模型网关 | Chat、Embedding、Rerank adapters | 调用上下文、有限重试、供应商 usage 和幂等计费 |
| 数据 | PostgreSQL 16、SQLAlchemy、Alembic | 权威事实、事务、约束、账号隔离和版本化迁移 |
| RAG | pgvector、LangChain Text Splitters | 文本切片、向量索引、余弦召回和可选重排 |
| 异步任务 | Celery、Redis、Celery Beat | OCR、RAG 索引、GitHub 分析和周期维护 |
| 文件 | MinIO / S3、python-docx、ReportLab | 原始简历和导出文件的对象存储与生成 |
| 文档识别 | pdfplumber、PDFium、RapidOCR、ONNX Runtime | DOCX/PDF 解析、扫描件检测和 OCR |
| 工程 | Docker Compose、Caddy、GitHub Actions | 本地复现、单机生产基线、HTTPS 和持续集成 |
| 质量 | pytest、Ruff、Node 回归脚本 | 业务、迁移、API、前端和发布基线验证 |

运行时固定为 Python `3.12.x`，Docker 基础镜像当前固定到 `python:3.12.13-slim`。生产依赖与开发质量工具分别锁定在 `requirements.lock` 和 `requirements-dev.lock`。

## 5. 项目亮点

1. **事实和推断分离**：只有候选人明确提供或确认的内容才能成为简历与匹配证据，职位要求不能反向污染候选人能力。
2. **结构化库和知识库联动删除**：项目、职位和简历删除沿 PostgreSQL 外键及应用服务边界清理，pgvector 不保留孤立索引。
3. **多租户隔离贯穿全链路**：数据库查询、对象键、RAG 检索、任务、工具轨迹和账单都携带账号归属。
4. **后台任务可恢复**：Redis 消息只包含 `task_key`，任务权威状态在 PostgreSQL；幂等键和原子认领防止重复执行。
5. **实时计量和余额账本**：只使用供应商确认的 Token usage 计费，调用 ID 防止重试重复扣费，余额不足时阻止新的模型调用。
6. **安全的外部内容入口**：职位截图仅在识别请求期间使用；GitHub 分析限制公开仓库、文件类型、归档大小和重定向目标。
7. **可执行的上线基线**：包含 CI、生产 Compose、HTTPS 反向代理、迁移门禁、备份与恢复脚本及上线前评测入口。

## 6. 目录结构说明

```text
Job-hunting Agent/
├─ src/job_hunting_agent/
│  ├─ web.py                  # FastAPI 路由、SSE 和页面入口
│  ├─ web_hardening.py        # CSRF、限流、安全头、请求 ID 与指标
│  ├─ agent.py                # LangChain Agent、提示词和工具注册
│  ├─ app.py                  # 业务门面与模块编排
│  ├─ storage.py              # PostgreSQL 领域仓储与事务逻辑
│  ├─ database_schema.py      # SQLAlchemy 表、约束和索引
│  ├─ model_gateway.py        # 模型调用、usage、重试与计费边界
│  ├─ rag.py                  # Embedding、Rerank 和切片协议
│  ├─ pgvector_rag.py         # pgvector 索引、检索和删除
│  ├─ background_tasks.py     # Celery 任务状态机和执行器
│  ├─ job_screenshot.py       # 职位截图多模态识别
│  ├─ github_project.py       # 公开仓库安全读取与筛选
│  ├─ resume_document.py      # DOCX/PDF 解析与 OCR
│  ├─ resume_writer.py        # 证据约束的定制简历内容
│  ├─ resume_exporter.py      # DOCX/PDF 导出
│  ├─ evals/                  # RAG 与对话语义评测
│  └─ web_static/             # Vue 页面、前端逻辑和样式
├─ alembic/versions/          # PostgreSQL 版本化迁移
├─ tests/                     # Python 测试和前端 Node 回归脚本
├─ scripts/
│  ├─ enterprise_acceptance.ps1 # 完整本地验收
│  ├─ backup.ps1                # PostgreSQL + MinIO 备份
│  └─ restore.ps1               # 受控恢复演练
├─ deploy/                    # Caddy 与生产环境变量模板
├─ docs/                      # ADR、研究、部署和学习文档
├─ compose.yaml               # 基础服务拓扑
├─ compose.dev.yaml           # 本地源码挂载与热更新
├─ compose.prod.yaml          # 单机生产覆盖配置
├─ Dockerfile
├─ requirements.lock
├─ requirements-dev.lock
├─ CONTEXT.md                 # 产品边界与权威上下文
└─ DECISION_MAP.md            # 已确认决策与后续队列
```

`.env`、数据库文件、缓存、构建产物和本地运行数据均被 Git 忽略。PostgreSQL、MinIO 和 Redis 默认使用 Docker named volume。

## 7. 运行步骤

### 7.1 环境要求

- Git
- Docker Desktop 或兼容的 Docker Engine
- Docker Compose v2
- 可用的 OpenAI-compatible Chat 模型配置
- 可选：Python 3.12、Node.js 22，用于宿主机测试

### 7.2 获取代码与配置

```powershell
git clone https://github.com/1055537213/Job-Hunting.git
Set-Location Job-Hunting
Copy-Item .env.example .env
```

编辑本地 `.env`，至少替换其中的模型 API、MinIO、Redis 和管理员引导占位配置。不要提交 `.env`，不要在日志、Issue 或截图中公开密钥。

计费相关配置：

```dotenv
JOB_AGENT_BILLING_PRICE_PER_MILLION_TOKENS_YUAN=25
JOB_AGENT_BILLING_STARTING_BALANCE_YUAN=0
JOB_AGENT_BILLING_LOW_BALANCE_THRESHOLD_YUAN=10
```

### 7.3 启动本地开发环境

```powershell
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
docker compose -f compose.yaml -f compose.dev.yaml ps
```

迁移服务会先执行 `alembic upgrade head`，成功后 Web、Worker 和 Beat 才会启动。浏览器访问：

```text
http://127.0.0.1:8000/login
```

常用检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
docker compose -f compose.yaml -f compose.dev.yaml logs --tail 100 web worker beat
```

开发覆盖已挂载 `src/` 和 `alembic/`。Python Web 代码会自动重载，前端静态文件刷新浏览器即可；Worker 或 Beat 代码变化后应重启对应容器：

```powershell
docker compose -f compose.yaml -f compose.dev.yaml restart worker beat
```

### 7.4 本地质量验收

先确保开发 PostgreSQL 正在运行，再安装锁定依赖：

```powershell
python -m pip install -r requirements.lock -r requirements-dev.lock
python -m pip install --no-deps -e .
.\scripts\enterprise_acceptance.ps1
```

也可以单独执行：

```powershell
python -m pytest -q
ruff check src tests alembic
python -m compileall -q src tests alembic
Get-ChildItem tests -Filter "frontend_*.mjs" | ForEach-Object { node $_.FullName }
```

RAG 黄金集准备完成后，将其加入统一验收：

```powershell
python -m job_hunting_agent.evals.rag_eval --write-example data\runtime\rag_eval_cases.example.json
.\scripts\enterprise_acceptance.ps1 -RagCases data\runtime\rag_eval_cases.example.json -AccountId 1 -TopK 5
```

### 7.5 单机生产基线

生产部署必须使用独立密码、独立数据卷、预创建对象存储 bucket 和不可变镜像标签。详细步骤见 [生产发布与恢复基线](docs/learning/production-release.md)。核心命令：

```powershell
docker build --tag $env:JOB_AGENT_IMAGE .
docker compose -f compose.yaml -f compose.prod.yaml config --quiet
docker compose -f compose.yaml -f compose.prod.yaml up -d --no-build
```

生产覆盖使用 PostgreSQL SCRAM 密码认证，内部服务不暴露宿主机端口，由 Caddy 提供 HTTPS。上线前至少完成一次：

```powershell
.\scripts\backup.ps1
.\scripts\restore.ps1 -BackupDirectory <backup-directory> -ConfirmRestore
```

## 8. 接口文档

启动后可直接访问 `/docs` 或 `/redoc` 查看由 FastAPI 生成的完整请求模型。主要接口如下：

| 模块 | 方法与路径 | 说明 |
| --- | --- | --- |
| 认证 | `POST /api/auth/register` | 注册普通账号 |
| 认证 | `POST /api/auth/login` | 登录并设置服务端 Session Cookie |
| 认证 | `GET /api/auth/me` | 获取当前账号、CSRF 和余额摘要 |
| 认证 | `POST /api/auth/logout`、`POST /api/auth/logout-all` | 退出当前设备或全部设备 |
| 档案 | `GET/POST /api/profiles` | 列出或创建候选人档案 |
| 档案 | `GET/DELETE /api/profiles/{candidate_id}` | 查看或删除档案 |
| 对话 | `POST /api/chat/stream` | SSE 流式 Agent 对话 |
| 对话 | `/api/chat/sessions`、`/api/chat/history` | 会话和历史记录管理 |
| 职位 | `POST /api/jobs`、`POST /api/jobs/screenshots` | 文本或截图导入职位 |
| 职位 | `GET /api/jobs`、`DELETE /api/jobs/{job_id}` | 列出或删除职位 |
| 匹配 | `GET /api/matches/{candidate_id}` | 返回排序结果和解释 |
| 项目 | `POST /api/projects/github` | 提交公开 GitHub 项目分析 |
| 项目 | `POST /api/projects/{record_id}/confirm` | 保存用户确认的项目内容 |
| 项目 | `GET /api/projects`、`DELETE /api/projects/{record_id}` | 列出或级联删除项目证据 |
| 简历 | `POST /api/resumes/upload` | 上传 DOCX/PDF 原始简历 |
| 简历 | `POST /api/resumes/{artifact_id}/tailor` | 生成职位定制简历 |
| 简历 | `GET /api/resumes/{artifact_id}/download` | 鉴权下载简历文件 |
| RAG | `GET /api/rag/search` | 账号隔离的检索调试接口 |
| 余额 | `GET /api/me/balance` | 当前账号余额与分页流水 |
| 余额 | `POST /api/me/balance/recharge` | 仅开发环境模拟充值 |
| 管理 | `/api/admin/usage/*`、`/api/admin/balance/*` | Token、余额和消费记录 |
| 管理 | `/api/admin/tools/traces*` | 工具调用任务与步骤详情 |
| 管理 | `/api/admin/observability/requests` | 进程内请求指标快照 |
| 管理 | `/api/admin/audit/events` | 管理员操作审计 |
| 运维 | `GET /api/health` | 数据库、存储、模型和队列健康摘要 |

已登录的写操作需要同源 Cookie 和 `X-CSRF-Token`。管理接口要求 `admin` 角色；所有资源接口还会校验账号归属。

## 9. 常见问题

### 为什么 `localhost:8000` 无法访问，但 `127.0.0.1:8000` 可以？

开发 Compose 显式绑定 IPv4 回环地址。部分 Windows 环境会把 `localhost` 优先解析到 IPv6 `::1`，请使用 `http://127.0.0.1:8000`。

### 修改前端后为什么页面没有变化？

先确认使用了 `compose.dev.yaml`，它会把 `src/` 挂载进容器。普通 HTML/CSS/JS 修改只需刷新；若浏览器仍持有旧资源，执行硬刷新。没有使用开发覆盖时，需要重新创建 Web 容器或重建镜像。

### 为什么新账号无法调用模型？

新账号初始余额为 `0`。本地开发可进入个人中心使用模拟充值；生产环境会禁用模拟入口，真实支付接入前应由受控运维流程充值。余额不足统一返回“余额不足，请先充值后重试。”

### 为什么项目确认后 RAG 任务失败？

项目卡片已经保存在 PostgreSQL，RAG 是后续派生索引任务。先检查余额、Worker、Redis 和模型网关日志；修复后可重新触发索引，不要重复创建项目。

### 删除项目后数据库和知识库会一起删除吗？

会。删除接口校验账号归属后删除项目卡片和关联长文本，pgvector 分块通过数据库级联删除；成功删除后同一 GitHub 仓库可以再次导入。

### 数据库结构应该怎样修改？

新增或修改表结构必须创建 Alembic revision，再执行 `alembic upgrade head`。Web 启动只校验 revision，不会绕过迁移自行建表。

### PostgreSQL、MinIO 和 Redis 数据保存在哪里？

本地默认保存在 Docker named volume。`docker compose down` 不会删除数据，`docker compose down -v` 会删除卷，执行前必须确认已备份。

### 如何查看服务故障？

先访问 `/api/health`，再查看 `web`、`worker`、`beat`、`postgres`、`redis` 和 `minio` 日志。管理员可在后台查看请求错误摘要、工具失败原因和审计记录。

## 10. TODO / 未来计划

- [ ] 接入真实支付渠道、签名 Webhook、充值订单和退款对账。
- [ ] 在上线前建立足量 RAG 黄金测试集，确定 Recall@K、MRR 和禁止召回阈值。
- [ ] 把请求指标接入 Prometheus/OpenTelemetry，并配置生产告警和长期趋势。
- [ ] 将进程内限流、短期 Agent 状态和指标迁移到共享基础设施，支持 Web 多副本。
- [ ] 将定制简历导出迁移到 Worker，并补充大文件和高并发容量测试。
- [ ] 建立严格类型检查基线，逐步消化第三方 stub 和内部 Protocol 类型债务。
- [ ] 完成依赖与镜像漏洞扫描、渗透测试、灾难恢复演练和密钥轮换流程。
- [ ] 在确定正式 Embedding 模型后评估 pgvector HNSW/IVFFlat 索引参数。

## 11. 联系方式与声明

- 问题反馈：[GitHub Issues](https://github.com/1055537213/Job-Hunting/issues)
- 架构边界：[CONTEXT.md](CONTEXT.md)、[DECISION_MAP.md](DECISION_MAP.md) 和 [ADR](docs/adr/)
- 部署说明：[生产发布与恢复基线](docs/learning/production-release.md)

本项目用于求职材料管理与工程实践。模型输出可能不准确，候选人应审核所有档案、项目、简历和 HR 回复后再使用。使用者需自行确认上传内容、第三方模型、对象存储和部署环境符合隐私、版权、平台条款及适用法律要求。本项目不承诺求职结果，也不提供绕过招聘网站登录、反爬或风控机制的能力。
