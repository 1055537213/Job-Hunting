# 求职助手 Agent

[![CI](https://github.com/1055537213/Job-Hunting/actions/workflows/ci.yml/badge.svg)](https://github.com/1055537213/Job-Hunting/actions/workflows/ci.yml)

一个面向求职准备场景的多账号 Agent 工作台。项目把候选人档案、职位信息、项目证据、简历文件和对话记忆组织成可追溯的数据链路，再通过 LangChain Agent、RAG 和 Celery 后台任务完成职位匹配、材料整理和定制简历生成。

> 项目遵循“候选人主动提供、系统分析、候选人确认、再用于求职表达”的边界。系统不会自动登录招聘平台、抓取隐藏接口、自动投递或自动发送招聘消息。

## 1. 项目简介（是什么 + 解决什么问题）

### 1.1 项目是什么

求职助手 Agent 是一个带有账号隔离、候选人档案管理、职位导入、项目证据分析、RAG 检索、简历生成和后台任务能力的模块化单体应用。

### 1.2 项目解决什么问题

求职资料通常分散在简历、职位描述、项目文档和聊天记录中；直接让模型自由生成，又容易把推断内容误当成候选人的真实经历。本项目解决以下问题：

- 用结构化档案保存学历、经历、技能、求职方向、城市和薪资偏好。
- 接收候选人主动带回的职位文本或截图，解析成可比较的职位信息。
- 分析公开 GitHub 仓库或候选人授权的本地项目材料，先形成待确认的项目经历卡片。
- 将确认事实和长文本材料建立账号隔离的 pgvector 索引，支持可追溯检索。
- 通过 Agent 对话完成档案维护、职位匹配和材料整理；复杂任务交给 Celery Worker。
- 生成独立的职位定制简历版本，不覆盖候选人原始事实和原始简历。
- 记录模型 Token 用量、余额变化、工具轨迹、审计事件和低敏 Prometheus 指标。

核心数据边界：PostgreSQL 是权威事实源，pgvector 是可重建的派生索引，Redis 负责任务传递、共享限流和并发租约。

## 2. 在线演示 / 效果截图（可选加分）

当前没有公开线上演示地址，也没有把本地运行截图作为仓库资产提交。启动本地环境后可以访问：

| 页面 | 地址 | 说明 |
| --- | --- | --- |
| 求职助手工作台 | http://127.0.0.1:8000/ | 登录、候选人档案、职位、项目和对话 |
| FastAPI 文档 | http://127.0.0.1:8000/docs | Swagger API 文档 |
| ReDoc | http://127.0.0.1:8000/redoc | 只读接口文档 |
| 健康检查 | http://127.0.0.1:8000/api/health | 数据库、队列、存储和模型摘要 |
| Prometheus | http://127.0.0.1:9090 | 仅绑定本机回环地址 |

如果要补充演示图，建议只提交脱敏后的界面截图，不要把 .env、用户简历、数据库导出、对象存储文件或验收报告提交到仓库。

## 3. 功能清单（列点式）

### 账号与安全

- 多账号隔离、候选人档案归属校验和 Session Cookie。
- 可选邮箱验证、密码重置、协议版本留痕和账号注销。
- Argon2id 密码哈希、CSRF、防重放、请求限流、安全响应头和请求 ID。
- 账号数据导出、密码修改、全部设备退出和匿名化删除。

### 候选人档案与 Agent

- 管理学历、工作经历、项目经历、技能熟练度、目标方向、城市和薪资偏好。
- 对话上下文恢复、历史压缩和 SSE 流式回复。
- 可选轻量意图路由器：仅对高置信度只读请求直达，歧义指代、多步骤、修改/确认表达、低置信度、超时和异常全部回退主 Agent。
- 路由直达次数、超时次数、固定回退原因和模型延迟直方图接入 Prometheus。
- 13 个 Agent 工具统一注册在 ToolRegistry；LangChain、直达路由、审计和可选 MCP adapter 共享名称、Schema、风险和执行元数据。

### 职位与匹配

- 粘贴职位文本导入，或上传职位截图进行多模态识别后复审。
- 职位字段解析、技能要求分类、重复导入检测和账号级数据隔离。
- 基于学历、经验、技能、城市、薪资和硬性限制进行可解释匹配与排序。

### 项目证据与知识库

- 公开 GitHub 仓库安全归档、默认分支快照和项目分析。
- 本地目录清单预扫描、哈希校验、分批上传、断点恢复和取消清理。
- 文本、代码、PDF、图片、CSV/XLSX、DOCX/PPTX 等来源分流提取。
- 项目分析结果先生成待确认卡片，候选人确认后才进入求职表达和 RAG。
- RAG 采用语义优先切分：按标题、段落、列表、代码块、表格和 PDF 页边界组织 Chunk，只有超长语义块才按句子和硬长度上限兜底拆分。
- 文字向量与视觉向量混合召回；视觉命中可以有限重开原图进行当前问题复核。

### 简历与后台任务

- DOCX、文字 PDF 和扫描 PDF 简历导入。
- OCR、RAG 索引、GitHub 分析、项目归档分析和职位定制简历导出由 Worker 执行。
- 任务具备幂等键、原子认领、进度、有限重试、错误摘要和失联回收。
- 生成的职位定制 DOCX/PDF 独立保存，不覆盖原始简历。

### 计费与运维

- Token 用量记录、余额扣减、充值订单、支付事件、管理员补款和追加式资金流水。
- Redis 共享限流、模型/截图并发租约和多 Web 副本运行基础。
- Prometheus 请求指标、意图路由指标、管理员工具轨迹和审计日志。
- 备份恢复、Worker 故障恢复、ClamAV 文件扫描和安全扫描验收脚本。

## 4. 技术栈（后端 / 前端 / 数据库 / 部署）

### 后端

- Python 3.12
- FastAPI + Uvicorn
- LangChain / LangGraph Agent
- Celery + Redis
- SQLAlchemy + Alembic
- pgvector

### 前端

- 原生 HTML、CSS、JavaScript
- Vue 3 Global Build（随 src/job_hunting_agent/web_static/vendor/ 提供）
- SSE 流式聊天

### 数据与基础设施

- PostgreSQL 16 + pgvector：结构化事实、任务、审计、账务和长文本登记
- MinIO 或 S3-compatible 对象存储：原始简历、导出文件和安全视觉派生文件
- Redis：Celery Broker、共享限流和并发租约
- Prometheus：低敏运行指标
- Caddy：生产 HTTPS 反向代理
- Docker Compose：开发、验收、恢复演练和单机生产部署

### 质量与安全

- Ruff、Pytest、Python compileall、Node 前端回归测试
- pip-audit、Trivy、CycloneDX SBOM
- ClamAV 生产文件扫描
- CI 位于 .github/workflows/ci.yml

## 5. 项目亮点（体现你做了什么，有何价值）

1. **事实与推断分离**：模型推断的项目职责、技能和成果不会自动变成候选人事实，降低简历夸大和事实污染风险。
2. **Agent 工具接口统一**：ToolRegistry 统一参数校验、执行、错误码和结果 envelope；LangChain 与轻量直达路由调用同一个 handler，避免双实现漂移。
3. **多租户隔离贯穿全链路**：数据库查询、对象键、RAG 检索、后台任务、工具轨迹、余额和审计事件都携带账号归属。
4. **后台任务可恢复**：任务状态以 PostgreSQL 为准，Redis 只传递任务键；幂等键和原子认领避免重复执行、重复扣费和重复导出。
5. **多模态证据链**：职位截图、项目图片和有限复杂 PDF 页面能够进入 OCR、视觉分析和视觉向量检索，同时保留来源定位；对图号、尺寸、公差等含数字术语提供精确补召回，避免只依赖向量相似度。
6. **轻量意图路由可控降级**：小模型只负责低风险、高置信度直达；对历史指代、多个意图、修改确认和身份缺失等风险场景保留主 Agent。
7. **生产基线可验收**：提供迁移门禁、备份恢复、Worker 崩溃恢复、多副本、文件扫描、Prometheus 和容器漏洞验收入口。
8. **计费事实可追溯**：供应商 usage、调用 ID、充值幂等键、管理员原因和余额流水能够关联排查。

## 6. 目录结构说明（项目文件夹介绍）

~~~text
.
├─ src/job_hunting_agent/
│  ├─ web.py                  # FastAPI API、页面入口、SSE 和鉴权
│  ├─ agent.py                # LangChain Agent、提示词、对话记忆和路由编排
│  ├─ app.py                  # 业务门面与模块编排
│  ├─ tool_registry.py        # 工具定义、上下文、统一结果和执行接口
│  ├─ job_hunting_tools.py    # 13 个求职工具的唯一注册位置
│  ├─ langchain_tool_adapter.py # ToolRegistry 到 LangChain 的 adapter
│  ├─ mcp_tool_adapter.py     # 可选 MCP 定义/结果 adapter，不启动 Server
│  ├─ models.py               # 领域记录、输入模型和结果模型
│  ├─ storage.py              # 领域仓储与事务逻辑
│  ├─ sqlalchemy_store.py     # PostgreSQL Store 实现与迁移版本检查
│  ├─ database_schema.py      # SQLAlchemy 表、约束和索引
│  ├─ config.py               # .env 和运行时配置读取
│  ├─ model_gateway.py        # 模型调用、usage、重试、并发和计费边界
│  ├─ intent_router.py        # 轻量路由、风险门禁、超时和路由指标
│  ├─ rag.py                  # Embedding、Rerank 和切片协议
│  ├─ pgvector_rag.py         # 文字 RAG 索引、检索和删除
│  ├─ pgvector_visual.py      # 视觉向量写入与校验
│  ├─ project_*.py             # GitHub、本地目录和项目证据处理
│  ├─ resume_*.py              # 简历解析、写作和 DOCX/PDF 导出
│  ├─ task_registry.py         # 独立后台任务目录和 Worker 分发接口
│  ├─ background_tasks.py      # 后台任务 handler、Celery 注册、重试和回收
│  ├─ worker.py / beat.py      # Worker 和 Beat 入口
│  ├─ account_*.py / auth.py   # 账号生命周期、邮件 Outbox 和认证
│  ├─ rate_limiting.py         # 内存/Redis 滑动窗口限流
│  ├─ concurrency_control.py   # 全局与账号级并发租约
│  ├─ file_scanning.py         # 本地安全扫描和 ClamAV 边界
│  └─ web_static/              # 前端页面、脚本、样式和 Vue 运行时
├─ alembic/                    # 数据库迁移
├─ tests/                      # Python 单元/集成测试和前端回归测试
├─ scripts/                    # 基准、备份恢复、扩容、扫描和企业验收脚本
├─ deploy/                     # Caddy、Prometheus 和生产环境模板
├─ docs/                       # ADR、架构决策、运行学习文档和发布基线
├─ compose.yaml                # 基础开发拓扑
├─ compose.dev.yaml            # 源码挂载和热更新覆盖
├─ compose.prod.yaml           # 单机生产覆盖
├─ compose.*-test.yaml         # 多副本、恢复、文件扫描和验收覆盖
├─ Dockerfile                  # Web/Worker/Beat/Migrate 共用镜像
├─ pyproject.toml              # 包元数据、入口命令和测试配置
├─ requirements.lock           # 运行时锁定依赖
└─ .env.example                # 脱敏配置模板
~~~

## 7. 运行步骤（一步一步写）

### 7.1 环境要求

- Docker Desktop 或 Docker Engine + Compose v2
- Git
- Python 3.12（宿主机测试可选）
- Node.js 22（前端回归测试可选）
- 一个 OpenAI-compatible Chat 模型配置

### 7.2 获取代码并配置环境变量

~~~powershell
git clone https://github.com/1055537213/Job-Hunting.git
cd Job-Hunting
Copy-Item .env.example .env
~~~

至少修改 .env 中的以下配置：

~~~dotenv
JOB_AGENT_LLM_PROVIDER=your-provider
JOB_AGENT_LLM_MODEL=your-chat-model
JOB_AGENT_LLM_API_KEY=your-api-key
JOB_AGENT_LLM_BASE_URL=https://api.example.com/v1
JOB_AGENT_OBJECT_STORAGE_ACCESS_KEY=your-minio-access-key
JOB_AGENT_OBJECT_STORAGE_SECRET_KEY=your-minio-secret
JOB_AGENT_REDIS_PASSWORD=your-redis-password
~~~

本地开发默认使用 JOB_AGENT_ACCOUNT_EMAIL_BACKEND=console、关闭强制邮箱验证，并使用本地对象存储/限流配置。生产环境必须使用独立 Secret、HTTPS、SMTP、ClamAV 和 Redis。

### 7.3 启动开发环境

~~~powershell
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
docker compose -f compose.yaml -f compose.dev.yaml ps
~~~

访问 http://127.0.0.1:8000/。查看日志：

~~~powershell
docker compose -f compose.yaml -f compose.dev.yaml logs --tail 100 web worker beat
~~~

停止服务但保留数据卷：

~~~powershell
docker compose -f compose.yaml -f compose.dev.yaml down
~~~

只有确认需要删除本地 PostgreSQL、MinIO、Redis 和 Prometheus 数据时，才执行：

~~~powershell
docker compose -f compose.yaml -f compose.dev.yaml down -v
~~~

### 7.4 本地质量检查

~~~powershell
ruff check src tests alembic
E:\Anaconda\python.exe -m compileall -q src tests alembic
E:\Anaconda\python.exe -m pytest -q
~~~

前端回归测试：

~~~powershell
Get-ChildItem tests -Filter 'frontend_*.mjs' | ForEach-Object { node $_.FullName }
~~~

配置和 Prometheus 检查：

~~~powershell
$env:JOB_AGENT_REDIS_PASSWORD='ci-redis-password'
docker compose --env-file .env.example -f compose.yaml config --quiet
docker run --rm --entrypoint /bin/promtool -v "$((Get-Location).Path)/deploy/prometheus:/etc/prometheus:ro" prom/prometheus:v3.13.1 check config /etc/prometheus/prometheus.yml
~~~

生产 Compose 配置检查还需要显式提供镜像、数据库密码和域名变量。CI 会执行同样的配置解析，
但不会启动生产服务或申请证书：

~~~powershell
$env:JOB_AGENT_REDIS_PASSWORD='replace-with-a-long-redis-password'
$env:JOB_AGENT_POSTGRES_PASSWORD='replace-with-a-long-url-safe-password'
$env:JOB_AGENT_IMAGE='ghcr.io/your-org/job-hunting-agent:release-tag'
$env:JOB_AGENT_DOMAIN='agent.example.com'
$env:JOB_AGENT_TLS_EMAIL='ops@example.com'
docker compose --env-file .env -f compose.yaml -f compose.prod.yaml config --quiet
~~~

专项验收脚本：

~~~powershell
.\scripts\validate_multi_replica.ps1
.\scripts\validate_worker_recovery.ps1
.\scripts\validate_backup_restore.ps1
.\scripts\validate_file_scanning.ps1
.\scripts\validate_rag_retrieval.ps1 -Python E:\Anaconda\python.exe
.\scripts\validate_rag_artifacts.ps1 `
  -Python E:\Anaconda\python.exe `
  -DatabaseUrl "postgresql+psycopg://job_agent@127.0.0.1:5432/job_agent"
.\scripts\security_scan.ps1
~~~

RAG 评测使用 `evals/rag/golden_suite.json` 中的固定跨行业语料。脚本会创建两个临时账号，
在真实 PostgreSQL/pgvector 中索引后统计最终 Top-N 的 Recall、Precision、nDCG、MRR、禁止召回率和标签分组指标，再自动
删除临时数据。默认使用 `.env` 中配置的正式 Embedding 与可选 Rerank，报告写入已忽略的
`data/eval-reports/`；`-EmbeddingMode local_hash` 只用于验证评测管线，不能代表语义召回质量。

真实文件评测分为两层。默认 `evals/rag/github_artifact_suite.json` 只有 12 份材料，定位是检查
下载、扫描、OCR/多模态、入库、检索和清理是否连通的冒烟集，不能作为上线准确率结论。正式
门禁使用 `evals/rag/github_hard_negative_suite.json`，扩充到 33 份材料和 33 条问题，加入同系列
工业图纸、施工基线/当前/变化图、关联财务表、相近医疗模块、相似设计海报和物流输入输出。
正式集的 `Top 5` 仅占语料 15.2%，31 条问题声明困难负样本，12 条属于保留验收集。

两套清单都固定 GitHub 仓库、40 位提交、文件大小、SHA-256 和许可证。运行时才从
`raw.githubusercontent.com` 下载，下载结果先校验，再经过项目清单规划、本地恶意内容检查、
OCR/多模态提取、长文本、pgvector 和检索指标链路。第三方原文件不会写入仓库；评测结束会
删除临时账号、余额流水、提取文字、向量和视觉副本，只在 `data/eval-reports/` 保留低敏报告。

默认同时使用当前 `.env` 的视觉模型、Embedding 和 Rerank，会产生真实模型费用。只有检查
评测程序本身时才使用 `-EmbeddingMode local_hash -VisualMode disabled`；该模式不能形成上线
质量结论。宿主机 `.env` 没有 `JOB_AGENT_DATABASE_URL` 时必须显式传 `-DatabaseUrl`；容器内的
`postgres` 主机名不能直接用于宿主机脚本。上游仓库文件即使在相同路径被替换，也会因提交或
内容摘要不匹配而在模型调用前失败。

正式发布前执行困难集：

~~~powershell
.\scripts\validate_rag_artifacts.ps1 `
  -BenchmarkRole release `
  -Python E:\Anaconda\python.exe `
  -DatabaseUrl "postgresql+psycopg://job_agent@127.0.0.1:5432/job_agent"
~~~

报告同时给出 `Recall@1/3/5`、最终 Top-N 的 Recall/Precision/nDCG、MRR、困难负样本命中率、行业
分组和 `development/holdout` 分层。默认线上检索漏斗为 Retriever Top-K=20、Reranker 最终 Top-N=5；开发集可用于切片、Embedding、Rerank 和检索参数调优；
保留集只用于阶段性验收，不能根据其中的失败问题逐条改查询或答案，否则会再次产生虚高结果。
`VisualMode configured` 会额外校验视觉分析产物数量，建立图片向量索引，并通过线上 `app.search_rag`
执行文字/图片混合召回和原图复核；报告中的 `visual_indexed` 必须与视觉项总数一致。`disabled` 只评估
文本层和 OCR 文字，不能用于声明图片结构或空间关系的召回能力。

RAG 在重排前会对查询中明确写出的排除条件进行规则过滤，例如“不要返回基线图”；过滤范围包含
来源路径和已提取证据正文。向量召回和重排使用移除否定从句后的正向查询，原始查询只用于结果过滤，
避免“不要返回的对象”反而污染语义相似度。明确包含“从……到……”“比较……并找到……”或“联合查询”
的多步骤问题会最多拆成 4 条阶段查询，每个阶段先取最佳证据，再合并去重，普通问题不会增加检索次数。
重排输入还会附带低敏的来源文件名、证据类型和页码，帮助模型区分同领域的表格、报告、图片和模块文件，
但最终返回给业务层的正文保持不变。Reranker 原始相关性分数会随证据写入评测报告，仅用于同一次重排
内的相对置信度校准，不作为跨模型通用的事实可信度。默认最高分低于 `0.65` 时返回空结果，其余候选
需达到本次最高分的 `86%`；Top-N 因此是最大返回数而不是必须凑满的数量，多步骤查询则按拆解阶段数
保留必要证据。两个阈值可通过 `JOB_AGENT_RERANK_MIN_RELEVANCE_SCORE` 和
`JOB_AGENT_RERANK_RELATIVE_SCORE_THRESHOLD` 调整，并且只能使用 development 集校准。对于包含尺寸、金额、编号等数值的查询，重排后还会优先保留与查询
数值一致且具有相同语义锚点的候选，并将同对象但数值不一致的候选后置。上述规则只修正排序和过滤，
不替代 Reranker，也不把评测集中的相似硬负样本误认为明确否定条件。

### 7.5 单机生产基线

生产部署必须使用正式 .env、独立数据卷、预创建对象存储 bucket、不可变镜像标签和 HTTPS 域名：

~~~powershell
Copy-Item .env.example .env
# 将 deploy/env.production.example 中的生产项合并到 .env，并填写模型、SMTP、数据库、Redis、对象存储和域名密钥。
# JOB_AGENT_IMAGE 必须指向已经通过 CI 构建和扫描的不可变镜像标签。
$env:JOB_AGENT_IMAGE='ghcr.io/your-org/job-hunting-agent:release-tag'
docker compose -f compose.yaml -f compose.prod.yaml config --quiet
docker compose -f compose.yaml -f compose.prod.yaml up -d --no-build
~~~

生产模板位于 `deploy/env.production.example`。它不会覆盖模型供应商配置，部署时应先从
`.env.example` 复制模型和业务配置，再用生产模板中的值替换开发项；`JOB_AGENT_DOMAIN`、
`JOB_AGENT_TLS_EMAIL`、`JOB_AGENT_PUBLIC_BASE_URL` 和 SMTP 配置必须使用真实生产值。
生产 Compose 会使用带密码的 PostgreSQL、Redis、ClamAV、MinIO、Caddy 和 Prometheus，
第一次启动前还要创建对象存储 bucket，并先执行 `docker compose ... run --rm migrate` 或让
Compose 的 `migrate` 服务完成迁移。生产环境的模拟充值接口会关闭，真实支付仍待后续接入。

生产上线前至少完成数据库迁移、健康检查、备份恢复演练、文件扫描验收和安全扫描。详细规则见 docs/learning/production-release.md。

## 8. 接口文档（如果是后端项目）

启动 Web 后，完整接口以 FastAPI 生成的 /docs 和 /redoc 为准。主要接口如下：

| 模块 | 方法与路径 | 说明 |
| --- | --- | --- |
| 认证 | POST /api/auth/register | 注册账号 |
| 认证 | POST /api/auth/login | 登录并设置 Session |
| 认证 | POST /api/auth/verify-email | 消费邮箱验证令牌 |
| 认证 | POST /api/auth/password-reset/request | 请求密码重置 |
| 账号 | GET /api/account/export | 导出本人数据 |
| 账号 | POST /api/account/delete | 删除或匿名化账号数据 |
| 档案 | GET/POST /api/profiles | 列出或创建候选人档案 |
| 对话 | POST /api/chat/stream | SSE Agent 对话 |
| 职位 | POST /api/jobs | 导入职位文本 |
| 职位 | POST /api/jobs/screenshots | 导入职位截图 |
| 匹配 | GET /api/matches/{candidate_id} | 返回职位匹配结果 |
| 项目 | POST /api/projects/github | 提交公开 GitHub 项目分析 |
| 项目 | POST /api/projects/local/manifest | 提交本地项目文件清单 |
| 项目 | POST /api/projects/{record_id}/confirm | 确认项目经历卡片 |
| 简历 | POST /api/resumes/upload | 上传原始简历 |
| 简历 | POST /api/resumes/{artifact_id}/tailor | 提交职位定制简历任务 |
| 简历 | GET /api/resumes/{artifact_id}/download | 下载简历文件 |
| 任务 | GET /api/tasks/{task_key} | 查询后台任务状态 |
| RAG | GET /api/rag/search | 账号隔离的检索接口 |
| 运维 | GET /api/health | 健康检查 |
| 运维 | GET /internal/metrics | Prometheus 指标，不加入公开 API 文档 |

已登录的写操作需要同源 Cookie 和 X-CSRF-Token；管理接口要求管理员角色；资源接口会校验账号归属。/internal/metrics 只应由内网 Prometheus 抓取，不应通过公网反向代理暴露。

## 9. 常见问题（FAQ）

### 为什么访问 localhost:8000 失败？

部分 Windows 环境会优先解析 IPv6 ::1。请使用 http://127.0.0.1:8000。

### 修改前端后为什么没有变化？

确认使用了 compose.dev.yaml。它会挂载 src/ 并开启 Uvicorn reload；浏览器仍有缓存时执行硬刷新。

### 为什么登录返回 403？

如果启用了邮箱验证，必须先消费验证邮件中的令牌；如果启用了协议同意，注册请求还必须带正确的条款和隐私版本。测试环境应使用独立临时配置，不要直接继承生产式 .env 开关。

### 为什么模型请求失败？

检查 JOB_AGENT_LLM_PROVIDER、JOB_AGENT_LLM_MODEL、API Key、Base URL 和余额配置。模型失败会经过超时、有限重试和熔断策略，详细原因查看 Web 日志和管理员工具轨迹。

### 为什么职位或项目没有立即完成？

OCR、GitHub 分析、RAG 索引和简历导出在队列开启时由 Worker 执行。检查 worker、beat、Redis 和 GET /api/tasks/{task_key}。

### 删除项目会不会留下向量或对象文件？

正常删除流程会按项目/候选人归属清理数据库记录、长文本、视觉派生文件、对象存储对象和对应 pgvector 索引；如失败，应先查看任务轨迹和数据库状态，不要手工直接删除生产卷。

### 如何查看意图路由效果？

访问管理端请求观测，或在 Prometheus 查询：

~~~promql
job_agent_intent_router_direct_total
job_agent_intent_router_fallback_total
job_agent_intent_router_timeouts_total
job_agent_intent_router_model_duration_seconds_bucket
~~~

### 项目现在是否已经运行 MCP Server？

没有。内部 ToolRegistry 是唯一工具实现，当前 LangChain Agent 和轻量路由都在进程内调用它。mcp_tool_adapter.py 只负责生成 MCP 兼容的 inputSchema、outputSchema、annotations 和 CallToolResult，默认仅导出只读工具；只有出现外部 Agent、独立进程或跨语言调用需求时，才需要在它之上启动 MCP Server。

### 系统会自动帮我投递或联系 HR 吗？

不会。系统只处理候选人主动带回的内容、分析和草稿生成；投递、发送消息和承诺类信息必须由候选人自行确认和执行。

## 10. TODO / 未来计划（体现成长）

- [ ] 接入真实支付渠道、签名 Webhook、退款状态机和渠道对账。
- [x] 建立首版隔离跨行业 RAG 黄金测试集，评估 Retriever Top-K、最终 Top-N、MRR、禁止召回和账号隔离。
- [x] 建立固定 GitHub 提交和哈希的跨行业真实文件端到端 RAG 评测。
- [ ] 持续扩充已授权的真实行业语料和难负样本，并按正式 Embedding 模型校准阈值。
- [ ] 接入 Alertmanager 通知、OpenTelemetry Trace 和集中日志平台。
- [ ] 出现明确外部调用方后，在现有 MCP adapter 上增加鉴权、授权和 Server 生命周期。
- [ ] 建立严格类型检查基线，逐步消化第三方 stub 和内部 Protocol 类型债务。
- [ ] 完成目标生产服务器的 ClamAV 验收、渗透测试、灾难恢复演练和密钥轮换流程。
- [ ] 根据正式 Embedding 模型评估 pgvector HNSW/IVFFlat 索引参数。
- [ ] 增加工业 PDF 表格/图注坐标、二进制 CAD 解析、父子 Chunk 和数值范围检索。
- [ ] 增加真实线上演示环境和脱敏效果截图。

已完成的企业化能力以代码、测试和验收脚本为准，包括邮箱生命周期、对象存储、知识资产、视觉证据、任务恢复、备份恢复、文件扫描、多副本限流、Prometheus 指标和轻量意图路由。

## 11. 联系方式 / 声明（可选）

- GitHub 仓库：https://github.com/1055537213/Job-Hunting
- Issue 反馈：https://github.com/1055537213/Job-Hunting/issues
- 架构边界：CONTEXT.md
- 决策记录：DECISION_MAP.md
- ADR：docs/adr/
- 发布与恢复：docs/learning/production-release.md

本项目仅提供求职准备、信息整理和材料生成辅助，不保证职位匹配结果、面试结果或录用结果。候选人应对提交给招聘平台的内容、真实性、隐私授权和最终发送行为负责。
