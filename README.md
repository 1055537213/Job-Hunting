# 求职助手 Agent

[![CI](https://github.com/1055537213/Job-Hunting/actions/workflows/ci.yml/badge.svg)](https://github.com/1055537213/Job-Hunting/actions/workflows/ci.yml)

一个面向求职准备场景的多账号 Agent 工作台。系统将候选人档案、职位、项目经历、简历文件和对话记忆组织为可追溯事实，并通过 LangChain Agent、RAG 与后台任务完成职位匹配、材料整理和定制简历生成。

> 当前项目处于个人开发者的企业化演进阶段，已具备 Docker Compose、PostgreSQL、pgvector、Redis、Celery、对象存储、计费流水、Prometheus 和 CI 基线。上传文件恶意扫描已接入；真实支付、集中日志和分布式 Trace 仍属于上线前工作。

## 1. 项目简介

### 1.1 项目是什么

求职助手 Agent 是一个带有多账号隔离能力的求职材料管理和分析系统。候选人可以在工作台中维护多个候选人档案，导入职位和项目证据，和 Agent 对话更新档案事实，并针对目标职位生成独立的定制简历版本。

### 1.2 项目解决什么问题

求职过程中，个人信息、职位要求、项目经历和简历版本通常分散在不同位置，直接让模型自由生成又容易混入未经确认的内容。本项目解决以下问题：

- 用结构化档案保存学历、经验、技能熟练度、目标方向、城市和薪资偏好。
- 审核用户粘贴的职位文本或主动上传的职位截图，拒绝无关内容和重复导入。
- 分析公开 GitHub 仓库快照或用户授权的本地项目目录，将推断出的技术与职责先交给用户按组确认。
- 将确认后的长文本建立账号隔离的 pgvector 索引，为匹配和简历生成提供可追溯证据。
- 通过 SSE 流式展示 Agent 回复，通过 Celery Worker 执行 OCR、项目分析和 RAG 索引。
- 记录供应商返回的 Token 用量并实时扣减余额；充值订单、支付事件、管理员补款和资金流水均可追溯。

PostgreSQL 是权威事实源；`rag_chunks` 是可重建的派生索引；Redis 只传递任务键。系统不会自动登录 BOSS、抓取隐藏接口、投递简历或发送招聘消息。

## 2. 在线演示 / 效果截图

### 2.1 在线演示

当前没有公开线上演示环境。启动本地 Docker Compose 后，可以访问以下页面：

| 页面 | 地址 | 用途 |
| --- | --- | --- |
| 登录与注册 | `http://127.0.0.1:8000/login` | 注册、邮箱验证、登录和密码找回 |
| 工作台 | `http://127.0.0.1:8000/workspace` | 档案、Agent 对话、职位、项目和简历 |
| 个人中心 | `http://127.0.0.1:8000/profile` | 余额、消费、改密、数据导出和账号注销 |
| 管理后台 | `http://127.0.0.1:8000/admin` | 账号用量、人工补款、余额账本、工具轨迹、请求观测和管理员审计 |
| Swagger | `http://127.0.0.1:8000/docs` | 交互式 API 文档 |

前端是随 FastAPI 一起发布的 Vue 单页应用，不需要单独启动 Node 开发服务器。仓库暂未提交公开部署截图，页面效果以本地运行结果和上表地址为准。

### 2.2 页面流程

1. 从登录页注册或登录账号。
2. 进入工作台建立候选人档案。
3. 通过文本或职位截图导入职位信息。
4. 通过公开 GitHub 链接或授权选择本地项目目录导入项目，也可以上传原始简历。
5. 在 Agent 对话中进行职位匹配、档案维护和材料整理。
6. 生成职位定制简历时查看任务状态，并在完成后下载 DOCX/PDF 文件。
7. 管理员从后台查看账号余额、Token 用量、工具调用流程、请求指标和审计记录。

## 3. 功能清单

### 账号与工作区

- 邮箱注册、一次性邮箱验证与密码重置、Argon2id 密码哈希、服务端 Session、CSRF 和登录限流。
- 注册会记录服务条款与隐私政策版本；用户可导出本人数据、主动改密并撤销全部设备，或删除求职数据并匿名化保留依法需要的财务事实。
- 登录页、工作台、个人中心和管理后台使用独立路由。
- 一个账号可创建多个候选人档案，每个档案可维护多个独立对话。
- 账号、档案、职位、项目、简历、RAG 和用量数据均按归属隔离。

### 候选人档案与对话

- 保存学历、经验、技能熟练度、证书、求职方向、城市和薪资偏好。
- 技能名称支持大小写与常见别名归一化，明确不会的技能不进入匹配和简历证据。
- 对话可更新档案事实；明确的“改为”语义会替换目标方向而不是追加。
- SSE 流式回复、持久化聊天记录、上下文恢复和超预算历史压缩。
- Compose/生产默认从 PostgreSQL 聊天历史恢复 Agent 短期上下文；只有显式配置内存后端时才使用单进程 `MemorySaver`。
- 任务过程以内嵌折叠区显示，任务完成后自动收起。

### 职位、项目与简历

- 粘贴职位文本导入，或上传职位截图后由多模态模型识别并由本地解析器复审。
- 职位、候选人、项目和原始简历按各自归属范围进行内容指纹去重。
- 按学历、经验和明确禁忌做硬筛选，按技能、方向、城市和薪资偏好排序并解释。
- GitHub 导入解析默认分支的 commit SHA，下载不可变快照，经过恶意文件扫描后保存到对象存储；同一提交幂等复用，新修订单独留存来源记录，分析结果相同则复用项目卡、业务内容变化才生成新卡片。
- 本地目录导入先由浏览器提交相对路径、大小和 SHA-256 清单，后端生成采集计划后再按最多 5 个文件一批传输；同一清单中断后可从未完成文件继续，也可取消并清理已产生的证据。`.env`、私钥、令牌、证书和依赖/构建目录在浏览器和服务端双重拦截。
- 单次项目最多选择 `120` 个文件、合计 `256 MiB`，并继续受文件类型数量、单文件大小、PDF 页数、表格行列和图片像素上限约束，避免整包导入无限占用内存和模型预算。
- 本地目录不保存用户电脑中的文件原件，也不会把整个目录一次性装入内存或对象存储；只保存提取结果、来源定位，以及图片/选中 PDF 页去 EXIF、缩放和重编码后的视觉派生副本。
- 项目证据按文本/代码、PDF 页面、图片、CSV/XLSX 工作表、DOCX/PPTX 和文本型工程文件分流；图片与有限复杂 PDF 页面会在 OCR 后交给当前多模态模型，按来源 ID 提取元素关系、表格、参数、单位、公差和适用对象。成功文字进入 `long_texts`，视觉派生副本进入 `visual_knowledge_items` 并由独立 Worker 生成图片向量。
- RAG 使用同一个多模态 Embedding 空间做文字与视觉双路召回；最多取前两项视觉命中重新打开经过校验的原图，让多模态模型按当前问题复核可见证据，而不是只依赖入库时的旧摘要。视觉命中可回溯到具体图片或 PDF 页，项目推断仍必须由用户按组确认。
- 删除项目或候选人时同步删除项目卡片、项目包、文件清单、对象原件、视觉派生副本、长文本事实和对应 pgvector 索引。
- 上传 DOCX、文字 PDF 或扫描 PDF；扫描件由 Worker OCR。
- 原始简历同时登记为统一知识资产和不可变首版；历史简历由 Alembic 原地补登记，文件不会搬迁或复制。
- 简历、职位截图、GitHub 归档和本地目录选中文件在进入解析、模型或 RAG 前先经过安全扫描；生产使用 ClamAV，未通过或无法扫描的文件不会进入下游处理。
- 基于职位和已确认候选人证据生成独立 DOCX/PDF 定制简历，不覆盖原始简历。

### RAG、后台任务与管理

- 结构化事实与长文本索引分离，Embedding 身份变化时不会混用旧向量。
- 检索先做账号、候选人、Embedding 身份和维度过滤，再合并文字切片与图片向量的余弦召回结果，可选 Rerank；Agent 工具与简历生成都显式传递当前候选人 ID。
- Celery 任务支持幂等键、原子认领、进度、有限重试、错误摘要和刷新后恢复。
- Beat 的失联回收与流水维护任务使用独立维护队列；普通 Worker 默认同时消费业务队列和维护队列，
  避免业务 Worker 故障时维护任务被一并阻塞。
- 队列开启时定制简历的模型、RAG、DOCX/PDF 生成均在 Worker 中执行；草稿和两个导出文件使用任务级幂等键，重试不会重复扣费或生成版本。
- Docker Web 使用 Redis 原子滑动窗口共享认证、模型、上传、管理和写请求额度，增加 Web 进程不会重复获得限流配额。
- Web 与 Worker 使用 Redis 租约共享 Chat、Embedding、Rerank 和截图并发额度，并按账号限制模型占用。
- 管理后台展示账号余额、消费流水、Token 明细、工具调用流程、请求指标和管理员审计；管理员可为自己或任意账号执行有原因、有操作者记录的人工补款。
- Token 与工具调用每账号最多保留 `5` 页、每页 `100` 条；余额流水、充值订单和支付事件属于财务事实，永久追加保存并按页读取。
- 新账号余额默认为 `0`；开发环境模拟充值也会生成幂等充值订单和支付事件，生产环境在接入真实支付前禁用用户模拟入口。

## 4. 技术栈

| 层次 | 技术 | 主要职责 |
| --- | --- | --- |
| 前端 | Vue 3、HTML、CSS、JavaScript | 多路由工作台、折叠任务过程、管理后台和响应式交互 |
| 通信 | FastAPI、SSE、JSON API | 认证 API、业务接口、文件下载和流式回复 |
| Agent | LangChain、LangGraph、OpenAI-compatible API | 模型适配、工具调用、会话状态和流式 Agent |
| 模型网关 | Chat、Embedding、Rerank adapters | 调用上下文、有限重试、供应商 usage 和幂等计费 |
| 数据 | PostgreSQL 16、SQLAlchemy、Alembic | 权威事实、事务、约束、账号隔离和版本化迁移 |
| RAG | pgvector、Qwen 多模态 Embedding、LangChain Text Splitters | 文本切片、图片向量、跨模态余弦召回和可选重排 |
| 异步任务 | Celery、Redis、Celery Beat | OCR、文字/视觉 RAG 索引、GitHub/项目整包分析、定制简历导出和周期维护 |
| 文件 | MinIO / S3、python-docx、ReportLab | 统一知识资产版本、项目包、原始简历和导出文件的对象存储与生成 |
| 文档识别 | pdfplumber、PDFium、RapidOCR、ONNX Runtime | DOCX/PDF 解析、扫描件检测和 OCR |
| 工程 | Docker Compose、Caddy、GitHub Actions | 本地复现、单机生产基线、HTTPS 和持续集成 |
| 质量与安全 | pytest、Ruff、pip-audit、Trivy | 业务回归、依赖漏洞门禁、镜像扫描和 SBOM |

运行时固定为 Python `3.12.x`，Docker 基础镜像固定到 Python `3.12.13-slim` 的不可变摘要。
生产依赖与开发质量工具分别锁定在 `requirements.lock` 和 `requirements-dev.lock`。

## 5. 项目亮点

1. **事实和推断分离**：只有候选人明确提供或确认的内容才能成为简历与匹配证据，职位要求不能反向污染候选人能力。
2. **结构化库和知识库联动删除**：项目、职位和简历删除沿 PostgreSQL 外键及应用服务边界清理，pgvector 不保留孤立索引。
3. **多租户隔离贯穿全链路**：数据库查询、对象键、RAG 检索、任务、工具轨迹和账单都携带账号归属。
4. **后台任务可恢复**：Redis 消息只包含 `task_key`，任务权威状态在 PostgreSQL；幂等键和原子认领防止重复执行。
5. **实时计量和资金闭环**：只使用供应商确认的 Token usage 计费；调用 ID、充值幂等键和数据库行锁防止重复扣费或重复到账，管理员补款与真实支付严格分开。
6. **安全的外部内容入口**：职位截图仅在识别请求期间使用；GitHub 快照限制归档大小、文件数、解压总量、路径、公开仓库和重定向目标；本地目录使用双端敏感路径拦截及上传前后哈希校验。
7. **可执行的上线基线**：包含 CI、生产 Compose、HTTPS 反向代理、迁移门禁、备份与恢复脚本及上线前评测入口。
8. **统一知识资产版本**：逻辑资料与不可变文件版本分离，账号归属、对象键、哈希、修订标签和处理状态集中管理。

## 6. 目录结构说明

```text
Job-hunting Agent/
├─ src/job_hunting_agent/
│  ├─ web.py                  # FastAPI 路由、SSE 和页面入口
│  ├─ account_lifecycle.py    # 一次性令牌、SMTP 邮件和账号找回边界
│  ├─ web_hardening.py        # CSRF、限流、安全头、请求 ID 与指标
│  ├─ rate_limiting.py        # 内存/Redis 滑动窗口与后端故障策略
│  ├─ concurrency_control.py   # 模型/截图全局与账号级共享并发租约
│  ├─ agent.py                # LangChain Agent、提示词和工具注册
│  ├─ app.py                  # 业务门面与模块编排
│  ├─ storage.py              # PostgreSQL 领域仓储与事务逻辑
│  ├─ database_schema.py      # SQLAlchemy 表、约束和索引
│  ├─ model_gateway.py        # 模型调用、usage、重试与计费边界
│  ├─ rag.py                  # Embedding、Rerank 和切片协议
│  ├─ pgvector_rag.py         # pgvector 索引、检索和删除
│  ├─ pgvector_visual.py      # 视觉对象校验与图片向量写入
│  ├─ background_tasks.py     # Celery 任务状态机和执行器
│  ├─ job_screenshot.py       # 职位截图多模态识别
│  ├─ project_visual.py       # 项目图片/PDF 页级视觉证据
│  ├─ github_project.py       # 公开仓库安全读取与筛选
│  ├─ file_scanning.py        # 本地验收扫描与生产 ClamAV 边界
│  ├─ resume_document.py      # DOCX/PDF 解析与 OCR
│  ├─ resume_writer.py        # 证据约束的定制简历内容
│  ├─ resume_exporter.py      # DOCX/PDF 导出
│  ├─ evals/                  # RAG 与对话语义评测
│  └─ web_static/             # Vue 页面、前端逻辑和样式
├─ alembic/versions/          # PostgreSQL 版本化迁移
├─ tests/                     # Python 测试和前端 Node 回归脚本
├─ scripts/
│  ├─ enterprise_acceptance.ps1 # 完整本地验收
│  ├─ validate_multi_replica.ps1 # 双 Web、共享限流和多目标采集验收
│  ├─ validate_worker_recovery.ps1 # Worker 失联回收、重投递和幂等验收
│  ├─ validate_backup_restore.ps1 # 隔离 PostgreSQL + MinIO 灾难恢复验收
│  ├─ security_scan.ps1          # Python 依赖、最终镜像与 SBOM 安全验收
│  ├─ validate_file_scanning.ps1 # ClamAV、EICAR、停机拒绝和隔离清理验收
│  ├─ backup.ps1                # PostgreSQL + MinIO 备份
│  └─ restore.ps1               # 受控恢复演练
├─ deploy/                    # Caddy、Prometheus 与生产环境变量模板
├─ docs/                      # ADR、研究、部署和学习文档
├─ compose.yaml               # 基础服务拓扑
├─ compose.dev.yaml           # 本地源码挂载与热更新
├─ compose.scale-test.yaml    # 临时多 Web 副本验收覆盖
├─ compose.acceptance.yaml    # Worker 故障恢复短时限验收覆盖
├─ compose.recovery-test.yaml # 无宿主机端口的隔离恢复演练覆盖
├─ compose.file-scan-test.yaml # 独立 ClamAV 与文件隔离验收覆盖
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

Docker Web/Worker 默认使用 Redis 数据库 `1` 保存短期限流窗口与并发租约，Celery broker
使用数据库 `0`；这些短期状态都不是业务事实源。Prometheus 会随基础 Compose 启动，
通过 Docker DNS 发现每个 Web 副本，并把各副本保留为独立采集目标；告警表达式再按整个
Web 服务聚合。副本可通过 Prometheus 的 `instance` 标签区分。本地监控页面和采集状态分别位于：

```text
http://127.0.0.1:9090
http://127.0.0.1:9090/targets
http://127.0.0.1:9090/alerts
```

当前只启用 Prometheus 指标、趋势和告警状态页面，没有启动 Alertmanager，也不会向外发送通知。

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

需要验证真实双 Web 副本、共享 Redis 限流和 Prometheus 多实例采集时执行：

```powershell
.\scripts\validate_multi_replica.ps1
```

脚本会临时移除 Web 的宿主机固定端口、启动两个副本，从 Worker 所在的 Compose 网络逐个
探测副本，并确认跨副本共享限流和两个 Prometheus target；结束或失败后都会恢复单 Web
开发拓扑与 `127.0.0.1:8000`。

需要验证 Worker 在执行中被强制停止后，Beat 是否能回收失联任务、重新排队，且幂等请求
不会生成第二条任务、Token 流水、余额流水或简历文件时执行：

```powershell
.\scripts\validate_worker_recovery.ps1
```

该脚本会创建一个带唯一邮箱的隔离验收账号，使用不读取用户数据的 `system_probe`，
验收结束后通过账号级级联删除清理测试记录。它会临时应用 `compose.acceptance.yaml`，
将回收窗口压缩到约一分钟；无论成功或失败都会尝试恢复普通开发拓扑。脚本需要 Docker
Engine 正在运行，并且当前镜像已经存在；代码只通过开发源码挂载更新时不需要重新构建镜像。

需要验证 PostgreSQL 与 MinIO 备份是否真的可以恢复时执行：

```powershell
.\scripts\validate_backup_restore.ps1
```

脚本使用唯一 Compose 项目名和独立数据卷，不会停止当前开发服务。它会写入一条数据库记录和
一个真实 MinIO 对象，生成备份，确认篡改后的归档会在停服前被 SHA-256 校验拒绝，再破坏两端
数据并执行恢复。最终报告保存在 `data/recovery-drills/<run>/recovery-report.json`，其中包含
本次观测 RTO；演练结束只清理对应的隔离容器和数据卷。

需要检查锁定依赖、最终 Debian 镜像并生成 CycloneDX SBOM 时执行：

```powershell
.\scripts\security_scan.ps1
```

CI 使用相同门禁并上传保留 14 天的 `security-reports`。规则、报告字段和漏洞例外要求见
[依赖与容器镜像安全扫描](docs/learning/security-scanning.md)。

需要验证真实 ClamAV、病毒库新鲜度、EICAR 隔离、扫描器故障和对象清理时执行：

```powershell
.\scripts\validate_file_scanning.ps1
```

脚本使用独立 Compose 项目和临时卷，不接触开发或生产数据；完整规则和报告说明见
[ClamAV 文件扫描验收](docs/learning/file-scanning-acceptance.md)。

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

生产覆盖使用 PostgreSQL SCRAM 密码认证，内部服务不暴露宿主机端口，由 Caddy 提供 HTTPS。生产 Compose 还会启动独立的 ClamAV 服务，Web 和 Worker 只有在扫描服务健康后才启动。上线前至少完成一次：

```powershell
.\scripts\backup.ps1
.\scripts\restore.ps1 -BackupDirectory <backup-directory> -ConfirmRestore
.\scripts\validate_backup_restore.ps1
```

恢复命令会在停服和覆盖数据之前校验 `manifest.json` 以及 PostgreSQL、MinIO 两个归档的
SHA-256。隔离脚本证明恢复机制可执行，但生产 RPO 仍取决于正式备份频率和异地副本策略。

生产 Compose 同时启动 Prometheus，默认保留 15 天请求趋势，并加载 Web 不可用、5xx 比例、
平均耗时、安全拦截和并发请求告警规则。Prometheus 页面只绑定服务器 `127.0.0.1:9090`，
应通过 SSH 端口转发访问；`/internal/metrics` 不会由 Caddy 暴露到公网。告警通知接收方需在
正式确定邮件、企业微信或其他值班渠道后，通过 Alertmanager 单独接入。Caddy 和 Prometheus
均通过 Docker DNS 动态发现 Web 副本，Caddy 轮询分发请求，告警按整个 Web 服务聚合。

## 8. 接口文档

启动后可直接访问 `/docs` 或 `/redoc` 查看由 FastAPI 生成的完整请求模型。主要接口如下：

| 模块 | 方法与路径 | 说明 |
| --- | --- | --- |
| 认证 | `POST /api/auth/register` | 注册普通账号 |
| 认证 | `POST /api/auth/verify-email`、`POST /api/auth/verification/request` | 验证邮箱或重发验证邮件 |
| 认证 | `POST /api/auth/password-reset/request`、`POST /api/auth/password-reset/confirm` | 请求并完成一次性密码重置 |
| 认证 | `POST /api/auth/login` | 登录并设置服务端 Session Cookie |
| 认证 | `GET /api/auth/me` | 获取当前账号、CSRF 和余额摘要 |
| 认证 | `POST /api/auth/logout`、`POST /api/auth/logout-all` | 退出当前设备或全部设备 |
| 账号 | `POST /api/account/password` | 校验当前密码后改密并撤销全部 Session |
| 账号 | `GET /api/account/export`、`POST /api/account/delete` | 导出本人数据或匿名化注销账号 |
| 档案 | `GET/POST /api/profiles` | 列出或创建候选人档案 |
| 档案 | `GET/DELETE /api/profiles/{candidate_id}` | 查看或删除档案 |
| 对话 | `POST /api/chat/stream` | SSE 流式 Agent 对话 |
| 对话 | `/api/chat/sessions`、`/api/chat/history` | 会话和历史记录管理 |
| 职位 | `POST /api/jobs`、`POST /api/jobs/screenshots` | 文本或截图导入职位 |
| 职位 | `GET /api/jobs`、`DELETE /api/jobs/{job_id}` | 列出或删除职位 |
| 匹配 | `GET /api/matches/{candidate_id}` | 返回排序结果和解释 |
| 项目 | `POST /api/projects/github` | 提交公开 GitHub 项目分析 |
| 项目 | `POST /api/projects/local/manifest` | 提交本地目录文件清单与哈希，获取后端采集计划 |
| 项目 | `POST /api/projects/local/{id}/files` | 分批上传采集计划选中的文件并增量写入 RAG |
| 项目 | `GET/DELETE /api/projects/local/{id}` | 恢复或取消未完成的本地目录采集 |
| 项目 | `POST /api/projects/local/{id}/complete` | 汇总已解析证据并生成待确认项目卡片 |
| 项目 | `POST /api/projects/{record_id}/confirm` | 保存用户确认的项目内容 |
| 项目 | `GET /api/projects`、`DELETE /api/projects/{record_id}` | 列出或级联删除项目证据 |
| 简历 | `POST /api/resumes/upload` | 上传 DOCX/PDF 原始简历 |
| 简历 | `POST /api/resumes/{artifact_id}/tailor` | 提交职位定制简历任务；队列开启时返回 `task`，关闭时同步返回文件 |
| 简历 | `GET /api/resumes/{artifact_id}/download` | 鉴权下载简历文件 |
| RAG | `GET /api/rag/search` | 账号隔离的检索调试接口 |
| 余额 | `GET /api/me/balance` | 当前账号余额与分页流水 |
| 余额 | `POST /api/me/balance/recharge` | 仅开发环境创建并结算模拟充值订单 |
| 余额 | `GET /api/me/recharge/orders` | 当前账号的充值订单 |
| 管理 | `/api/admin/usage/*`、`/api/admin/balance/*` | Token、余额和消费记录 |
| 管理 | `POST /api/admin/accounts/{id}/balance/credit` | 管理员人工补款，要求原因和幂等键 |
| 管理 | `/api/admin/recharge/orders*` | 充值订单和低敏支付事件排障 |
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

新账号初始余额为 `0`。本地开发可进入个人中心使用模拟充值；生产环境会禁用用户模拟入口。管理员仍可在后台为自己或指定账号人工补款，金额、原因、操作者、目标账号和补款前后余额会在同一事务中写入资金流水与管理员审计。余额不足统一返回“余额不足，请先充值后重试。”

### 为什么项目确认后 RAG 任务失败？

项目卡片已经保存在 PostgreSQL，RAG 是后续派生索引任务。先检查余额、Worker、Redis 和模型网关日志；修复后可重新触发索引，不要重复创建项目。

### 删除项目后数据库和知识库会一起删除吗？

会。删除接口校验账号归属后删除项目卡片和关联长文本，pgvector 分块通过数据库级联删除；GitHub 导入还会删除快照、文件清单、知识资产版本和对象原件，本地目录导入会删除文件证据。成功删除后同一来源可以再次导入。

### 数据库结构应该怎样修改？

新增或修改表结构必须创建 Alembic revision，再执行 `alembic upgrade head`。Web 启动只校验 revision，不会绕过迁移自行建表。

### PostgreSQL、MinIO 和 Redis 数据保存在哪里？

本地默认保存在 Docker named volume。`docker compose down` 不会删除数据，`docker compose down -v` 会删除卷，执行前必须确认已备份。

### 如何查看服务故障？

先访问 `/api/health`，再查看 `web`、`worker`、`beat`、`postgres`、`redis` 和 `minio` 日志。
管理员可在后台查看请求错误摘要、工具失败原因和审计记录；生产运维可在 Prometheus 查看
15 天趋势和当前告警状态。

## 10. TODO / 未来计划

- [x] 建立充值订单、支付事件、幂等到账和管理员人工补款基础链路。
- [ ] 接入真实支付渠道、签名 Webhook、退款状态机和渠道对账。
- [x] 完成邮箱验证、密码找回、协议版本留痕、数据导出和匿名化账号注销。
- [ ] 在上线前建立足量 RAG 黄金测试集，确定 Recall@K、MRR 和禁止召回阈值。
- [x] 建立统一知识资产和不可变文件版本底座，并迁移现有原始简历关系。
- [x] 在现有项目导入面板接入本地目录预扫描、后端采集计划和按需分批传输，不新增独立上传入口。
- [x] GitHub 导入保存 commit 快照和多类型文件清单，并与项目证据解析、删除和 RAG 生命周期贯通。
- [x] 项目图片与有限复杂 PDF 页面接入多模态模型，保留页级元素关系及结构化参数/公差元数据，失败时回退 OCR。
- [x] 为项目图片和选中 PDF 页保存安全视觉派生副本，建立图片向量并接入文字+视觉混合召回。
- [x] 视觉向量命中后限量重开原图，按当前查询执行多模态证据复核。
- [ ] 接入工业 PDF 表格/图注坐标、二进制 CAD 解析、父子 Chunk 和数值范围检索。
- [x] 把低敏请求指标接入 Prometheus，配置单机生产告警规则和 15 天趋势保留。
- [ ] 接入 Alertmanager 通知渠道、OpenTelemetry 分布式 Trace 和集中日志平台。
- [x] 将 Web 请求限流迁移到 Redis 原子滑动窗口，并明确 Redis 故障时的分组降级策略。
- [x] 将模型/截图并发名额迁移到 Redis 租约，并同时限制全局和单账号占用。
- [x] 完成 Web 多副本流量验证、Caddy 动态后端发现与 Prometheus 多实例采集。
- [x] 将生产/Compose 的短期 Agent 状态迁移到 PostgreSQL 聊天历史，避免副本切换时依赖单进程内存。
- [x] 为 Chat、Embedding 和 Rerank 增加超时、有限重试和进程内熔断；熔断期间统一返回可重试的 503。
- [x] 将定制简历导出迁移到 Worker，并用任务级幂等键保护重试；Worker 失联任务会由 Beat 回收。
- [x] 增加真实 Worker 崩溃恢复、长任务重投递和任务级幂等验收脚本；上线前仍需在目标服务器执行并记录结果。
- [x] 增加隔离 PostgreSQL + MinIO 备份恢复演练、归档防篡改校验和 RTO 报告。
- [x] 接入 Python 依赖与最终镜像漏洞门禁，固定扫描器摘要并生成 CycloneDX SBOM。
- [ ] 建立严格类型检查基线，逐步消化第三方 stub 和内部 Protocol 类型债务。
- [ ] 完成渗透测试、目标服务器灾难恢复演练和密钥轮换流程。
- [x] 接入上传文件和 GitHub 归档的扫描隔离状态；生产 Compose 使用 ClamAV，扫描失败禁止进入下游处理。
- [x] 增加 ClamAV 病毒库新鲜度、EICAR、扫描服务故障恢复和隔离文件删除的独立验收脚本，并在本地 Docker 通过。
- [ ] 在目标生产服务器再次执行 ClamAV 验收并保存报告。
- [ ] 在确定正式 Embedding 模型后评估 pgvector HNSW/IVFFlat 索引参数。

## 11. 联系方式 / 声明

- 问题反馈：[GitHub Issues](https://github.com/1055537213/Job-Hunting/issues)
- 架构边界：[CONTEXT.md](CONTEXT.md)、[DECISION_MAP.md](DECISION_MAP.md) 和 [ADR](docs/adr/)
- 部署说明：[生产发布与恢复基线](docs/learning/production-release.md)

本项目用于求职材料管理与工程实践。模型输出可能不准确，候选人应审核所有档案、项目、简历和 HR 回复后再使用。使用者需自行确认上传内容、第三方模型、对象存储和部署环境符合隐私、版权、平台条款及适用法律要求。本项目不承诺求职结果，也不提供绕过招聘网站登录、反爬或风控机制的能力。
