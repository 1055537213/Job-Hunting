# 求职助手 Agent

## 1. 项目概述

求职助手 Agent 是一个面向 BOSS 直聘求职场景的网页应用。一个账号可以创建多个候选人档案，每个档案可以建立多个独立对话。系统通过 LangChain Agent 组合候选人资料、职位信息、项目证据、简历文件和对话记忆，为不同求职者提供通用的职位匹配与材料生成能力。

当前核心功能：

- 保存学历、经验年限、技能、证书、目标城市、薪资和不可接受条件等结构化档案。
- 审核并导入用户复制的 BOSS 职位文本，或用户主动上传截图后由多模态模型先判定、再转写的职位文本；非招聘图片和无关文本不会保存。
- 按学历、经验、技能和明确不可接受条件淘汰职位，再用普通偏好进行排序。
- 分析用户主动提供的公开 GitHub 仓库，生成待确认项目经历卡片；确认后写入长文本事实源并自动创建 RAG 增量索引任务。
- 上传 DOCX、文字版 PDF 或扫描版 PDF 简历，提取正文并保存文件版本；扫描件由后台 Worker 执行 OCR。
- 根据目标职位生成职位定制简历草稿和 DOCX/PDF 文件，不覆盖候选人档案或原始简历。
- 根据 HR 问题生成可编辑回复草稿，并保持候选人事实与模型推断的边界。
- 持久化聊天记录，启动时恢复上下文；超出上下文预算时压缩旧消息。
- 记录聊天、Embedding 和 Rerank 的 Token 用量，为后台统计和后续按量计费提供数据。

系统当前采用用户主动提供数据的方式接入 BOSS 直聘，不自动登录、抓取隐藏接口、投递简历或发送消息。

## 2. 技术栈

| 分类 | 技术 | 作用 |
| --- | --- | --- |
| 前端 | Vue、HTML、CSS、JavaScript | 构建登录页、候选人工作台、流式聊天、职位与简历管理界面 |
| 前端 | Server-Sent Events、Markdown 渲染 | 接收流式 Agent 回复并展示表格、列表和代码块 |
| 后端 | Python 3.12.13、FastAPI、Uvicorn | 提供网页服务、认证、业务 API、文件上传下载和流式响应 |
| 后端 | LangChain、LangGraph | 创建 Agent、注册工具、编排模型调用和管理会话状态 |
| 后端 | Celery、Redis | 异步执行 OCR、RAG 索引和 GitHub 项目分析任务 |
| 后端 | RapidOCR、PDFium、pdfplumber、python-docx、ReportLab | 识别、解析和生成 DOCX/PDF 简历文件 |
| 数据库 | PostgreSQL、SQLAlchemy | 保存账号、候选人档案、职位、会话、任务、文件元数据和 Token 用量 |
| 数据库 | pgvector | 保存 RAG 文本向量并执行账号隔离的语义检索 |
| 数据库 | Alembic | 以版本化迁移管理数据库表、约束和索引 |
| 存储 | MinIO、S3-compatible API | 保存上传的原始简历和生成的职位定制文件 |
| 开发工具 | Docker、Docker Compose | 统一启动数据库、对象存储、消息队列、迁移、Web 和 Worker |
| 开发工具 | pytest | 验证业务规则、API、数据库迁移、RAG、后台任务和前端回归行为 |

### Python 运行时基线

- Docker 基础镜像固定为 `python:3.12.13-slim`，避免 `python:3.12-slim` 这类浮动标签在未来构建时自动切换补丁版本。
- 本项目在宿主机直接使用 `E:\Anaconda\envs\langchain1.2`，其中运行的是 Python 3.12.13；该环境也是本地测试和 `pip-tools` 的运行环境。
- 运行时依赖由 `requirements.lock` 固定，解释器版本由本地 Conda 环境和 Docker 基础镜像共同固定。

## 3. 项目架构

### 整体架构与模块关系

```text
浏览器中的 Vue 页面
        |
        | HTTP / SSE
        v
FastAPI Web API
        |
        v
JobHuntingApp 业务门面
        |
        +---------------- LangChain Agent ----------------+
        |                    |                             |
        |                    v                             v
        |             Model Gateway                 Agent 工具集
        |          聊天 / Embedding / Rerank     档案 / 职位 / 项目 / 简历
        |                                                  |
        +--------------------+-----------------------------+
                             |
             +---------------+----------------+
             |               |                |
             v               v                v
        PostgreSQL       MinIO 对象存储    Redis 消息队列
        结构化事实源      简历二进制文件        |
        long_texts                              v
        rag_chunks                         Celery Worker
        background_tasks                  OCR / RAG / GitHub 分析
```

`PostgreSQL` 是系统的权威事实源。`long_texts` 保存可追溯的长文本原文，`rag_chunks` 是通过 pgvector 建立的可重建派生索引。Redis 只传递 `task_key`，后台任务的状态、参数和结果摘要仍保存在 PostgreSQL；简历正文和仓库源码不会写入 Redis。

### 主要数据流

1. 用户登录后选择候选人档案和对话会话。
2. FastAPI 接收聊天、职位文本、用户主动上传的职位截图、GitHub 链接或简历文件，并校验账号与候选人归属；截图模型先返回是否为职位和置信度，再由本地职位解析器复审。截图只在识别请求期间传给模型，服务端不会访问其来源链接或持久化截图本体。
3. LangChain Agent 根据用户意图调用档案、职位、项目、RAG、简历或 HR 回复工具。
4. 可精确比较的字段写入 PostgreSQL；职位同时记录用户填写的来源链接、导入方式和接收时间；项目描述、职位全文和简历正文登记到 `long_texts`。
5. OCR、RAG 索引和 GitHub 分析等可持久化耗时操作先创建 `background_tasks` 记录，再由 Redis/Celery Worker 认领；重复消息只有一个 Worker 可以执行。截图识别为了不持久化图片，采用带并发上限的前台线程调用。
6. RAG Worker 切分长文本、调用 Embedding，并将向量写入 `rag_chunks`；检索时先按账号和候选人过滤。
7. Agent 结合结构化事实、职位要求和 RAG 证据生成匹配解释、简历草稿或 HR 回复。
8. 前端通过 SSE 接收聊天输出，通过任务 API 轮询 OCR、GitHub 分析和 RAG 索引状态。

## 4. 目录结构

```text
Job-hunting Agent/
├─ src/job_hunting_agent/
│  ├─ web.py                     # FastAPI、认证、业务 API 与 SSE 入口
│  ├─ agent.py                   # LangChain Agent、提示词与工具注册
│  ├─ app.py                     # 应用服务门面和模块编排
│  ├─ storage.py                 # PostgreSQL 领域读写逻辑
│  ├─ sqlalchemy_store.py        # SQLAlchemy 连接与事务适配
│  ├─ database_schema.py         # PostgreSQL/pgvector 表结构定义
│  ├─ models.py                  # 领域模型和 API 数据对象
│  ├─ model_gateway.py           # 模型调用、重试、调用 ID 和 Token 用量
│  ├─ pgvector_rag.py            # pgvector 索引与语义检索
│  ├─ background_tasks.py        # Celery 任务状态机和执行器
│  ├─ worker.py                  # Celery Worker 入口
│  ├─ job_screenshot.py          # 职位截图校验、多模态审核与短生命周期转写
│  ├─ deduplication.py           # 内容规范化、SHA-256 指纹与重复资源错误
│  ├─ github_project.py          # GitHub URL 校验、归档读取与安全筛选
│  ├─ resume_document.py         # DOCX/PDF 解析与 OCR
│  ├─ resume_writer.py           # 职位定制简历内容生成
│  ├─ resume_exporter.py         # DOCX/PDF 简历导出
│  └─ web_static/                # Vue 页面、样式、城市数据和前端脚本
├─ alembic/
│  └─ versions/
│     └─ 20260814_0003_content_deduplication.py # 内容指纹与职位来源追溯迁移
├─ tests/                        # Python 测试与前端回归脚本
├─ docs/
│  ├─ adr/                       # 架构决策记录
│  ├─ learning/                  # 技术栈学习与操作说明
│  └─ research/                  # BOSS 接入与职位标准化研究
├─ compose.yaml                  # 完整本地容器拓扑
├─ compose.dev.yaml              # 源码挂载和热更新覆盖配置
├─ Dockerfile                    # Web/Worker 镜像构建
├─ requirements.lock             # pip-tools 生成的精确运行时依赖版本
├─ alembic.ini                   # Alembic 配置
├─ pyproject.toml                # Python 依赖、包配置和命令入口
├─ .env.example                  # 环境变量模板，不包含真实密钥
├─ CONTEXT.md                    # 产品边界与领域上下文
└─ DECISION_MAP.md               # 已确认决策和后续问题队列
```

运行数据不提交到 Git。PostgreSQL、MinIO 和 Redis 数据分别保存在 Docker named volume 中；`.env`、缓存、构建产物和本地运行文件由忽略规则排除。

## 5. 核心文件说明

### 项目入口和配置

- `src/job_hunting_agent/web.py`：网页主入口，提供注册登录、候选人档案、会话、职位、匹配、项目、简历、RAG、后台任务和管理员 API。
- `src/job_hunting_agent/worker.py`：独立 Worker 入口，注册 Celery 任务并消费 Redis 队列。
- `src/job_hunting_agent/config.py`：读取 `.env`，校验数据库、对象存储、队列、模型、Embedding、Rerank、记忆和 Session 配置。
- `compose.yaml`：启动 PostgreSQL、MinIO、Redis、Alembic 迁移、Web 和 Worker。首次运行前需要将 `.env.example` 复制为 `.env`，替换占位凭据和模型配置，然后执行 `docker compose up -d --build`。
- `compose.dev.yaml`：本地开发覆盖配置，挂载源码并开启 Web 热更新。
- `pyproject.toml`：声明依赖、包数据以及 `job-agent-web`、`job-agent-worker` 命令入口。
- `requirements.lock`：由 `pip-tools` 根据 `pyproject.toml` 生成的运行时依赖锁定文件；Docker
  构建使用它安装依赖，修改 `pyproject.toml` 后必须重新生成。

### 核心业务实现

- `src/job_hunting_agent/app.py`：连接存储、Agent、RAG、项目分析、职位匹配、简历处理和后台任务，是 Web 与工具层共用的业务门面。
- `src/job_hunting_agent/agent.py`：使用 LangChain 创建 Agent，定义系统提示词并注册候选人资料、职位、GitHub 项目、简历和 HR 回复工具。
- `src/job_hunting_agent/matcher.py`：实现学历硬门槛、经验差距淘汰、不可接受条件淘汰、技能评分和普通偏好排序。
- `src/job_hunting_agent/job_parser.py`、`job_screenshot.py`：分别复审职位文本、校验用户截图；截图必须经多模态模型明确判断为职位后才进入解析器。
- `src/job_hunting_agent/deduplication.py`：将内容规范化为 SHA-256 指纹；职位和候选人档案在账号内去重，项目经历和原始简历在同一候选人内去重，避免共享账号中不同人的材料相互阻塞。
- `src/job_hunting_agent/conversation_ingestion.py`：判断对话内容应写入结构化档案、长文本知识来源或仅保留为聊天消息。
- `src/job_hunting_agent/conversation_memory.py`：恢复持久化对话，计算上下文预算并压缩较早消息。
- `src/job_hunting_agent/project_analyzer.py`、`github_project.py`：从受控项目文本提取技术栈和功能线索；GitHub 模块只访问公开仓库官方端点，不执行仓库代码。
- `src/job_hunting_agent/resume_writer.py`、`resume_exporter.py`：生成证据约束的职位定制草稿，并导出独立 DOCX/PDF 文件。

### 数据模型和 API

- `src/job_hunting_agent/models.py`：定义账号、候选人、职位、项目卡片、简历、聊天、RAG、任务和 Token 用量等领域对象。
- `src/job_hunting_agent/database_schema.py`：定义目标 PostgreSQL 表、外键、唯一约束、状态检查、职位来源字段和 pgvector 字段。
- `src/job_hunting_agent/storage.py`：实现账号隔离的领域查询、写入和后台任务原子认领。
- `src/job_hunting_agent/sqlalchemy_store.py`：将仓储接口连接到 SQLAlchemy Engine，并统一 PostgreSQL 参数与事务行为。
- `alembic/versions/`：保存可审计的数据库升级脚本；Web 启动时只校验版本，不自行建表。
- `src/job_hunting_agent/web.py`：将核心业务暴露为 JSON API、文件下载响应和 SSE 流式聊天接口。

### 关键组件和服务模块

- `src/job_hunting_agent/model_gateway.py`、`llm.py`：统一聊天、Embedding、Rerank 的供应商配置、有限重试、调用 ID 和 Token usage 记录；兼容通用 OpenAI-compatible 模型与旧版 DeepSeek 思考参数。
- `src/job_hunting_agent/rag.py`、`pgvector_rag.py`：负责文本切分、Embedding 协议、可选 Rerank、向量写入和来源可追溯检索。
- `src/job_hunting_agent/object_storage.py`：通过 S3-compatible 接口读写 MinIO，数据库只保存对象键、哈希、版本和归属。
- `src/job_hunting_agent/task_queue.py`：封装 Celery 投递，队列消息只包含安全的 `task_key`。
- `src/job_hunting_agent/background_tasks.py`：原子认领任务并处理进度、重试、失败恢复、OCR、RAG 和 GitHub 分析。
- `src/job_hunting_agent/resume_document.py`：校验文件类型和大小，解析 DOCX、文字 PDF，并为扫描 PDF 提供 OCR 流程。
- `src/job_hunting_agent/web_static/index.html`、`app.js`、`styles.css`：实现登录、工作台、Markdown、SSE、任务轮询和响应式界面。
- `tests/`：覆盖数据库迁移、认证隔离、职位匹配、Agent、RAG、对象存储、后台任务、简历和网页回归行为。
