# 求职助手 Agent

## 项目概述

求职助手 Agent 是一个面向 BOSS 直聘求职场景的本地求职辅助系统。它把候选人的结构化档案、项目证据、职位信息和求职对话组织到同一个工作区中，帮助候选人完成以下工作：

- 保存学历、经验年限、技能、证书、城市偏好、薪资要求和不可接受条件。
- 读取候选人主动提供的本地项目目录，分析技术栈、功能线索和项目经历草稿。
- 保存候选人主动复制的 BOSS 职位文本，解析职位名称、城市、薪资、学历、经验和技能要求。
- 按硬性条件和普通偏好计算职位匹配结果，并解释淘汰原因、短板和风险。
- 上传 DOCX、文字 PDF 或扫描 PDF 简历，扫描件通过本地 OCR 提取文字。
- 根据目标职位生成证据约束的简历草稿和独立 DOCX/PDF 文件，不覆盖候选人档案或原文件。
- 根据候选人主动带回的 HR 问题生成可编辑回复草稿。
- 在对话中自动判断内容应保存到 SQLite 结构化存储还是长文本 RAG 知识库。

系统的外部操作边界如下：

- 不自动登录 BOSS 直聘。
- 不自动爬取职位页面。
- 不自动投递简历。
- 不自动发送 HR 消息。
- 不把未确认的项目职责、技能熟练度或成果数字当成已确认事实。

候选人需要自己在 BOSS 直聘中查看职位，然后复制可见职位文本或提供本地文件。Agent 只负责本地解析、检索、匹配和生成草稿。

## 技术栈

- 开发语言：Python 3.12 及以上。
- Web 服务：FastAPI、Uvicorn。
- Agent 编排：LangChain 1.x、LangGraph。
- 模型接入：内部 Model Gateway 统一调用 OpenAI-compatible Chat API，通过 `.env` 配置供应商标签、模型、密钥和地址。
- Embedding：支持 OpenAI-compatible、provider-native 多模态协议；未配置时回退到本地 hash embedding。
- Rerank：支持常见 `/rerank` 协议或 provider-native 协议，对向量召回候选再次排序；未配置时保持纯向量检索。
- 结构化存储：SQLite。
- 语义检索：Chroma、LangChain Chroma 集成、LangChain 文本切分器。
- 简历文档：python-docx、pdfplumber、PDFium、RapidOCR、ONNX Runtime、ReportLab。
- 前端：Vue 3 本地静态构建、SSE 流式聊天、Markdown 渲染。
- 认证：Argon2id 密码哈希、HttpOnly Session Cookie。
- 容器化：Docker、Docker Compose；当前用于复现 SQLite + Chroma 本地开发环境。
- 测试：pytest。

## 项目架构

系统采用“结构化事实源 + 语义检索索引 + Agent 工具调用”的架构。

```text
Vue 3 Web 前端 / CLI
          |
          v
FastAPI Web API / CLI 入口
          |
          v
JobHuntingAgent
  LangChain create_agent
  LangGraph 会话状态
          |
          v
内部 Model Gateway
  模型配置、重试策略、call_id、Token 用量
          |
          v
工具层
  档案工具
  项目分析工具
  职位导入与匹配工具
  简历上传、改写与导出工具
  HR 回复草稿工具
          |
          +----------------------+
          |                      |
          v                      v
SQLite 结构化事实源       Chroma RAG 索引          受控文件目录
学历、经验、技能、证书     项目描述、成果材料、       原始 DOCX/PDF、
偏好、职位字段、文件元数据  职位全文、简历片段         职位定制 DOCX/PDF
```

### 数据流

1. 用户登录后选择候选人档案和求职会话。
2. 用户输入资料、职位文本、项目目录、简历文件或 HR 问题。
3. LangChain Agent 判断需要调用的工具。
4. 工具把可精确比较的字段写入 SQLite，把长文本切分后写入 Chroma。
5. 查询先由 Chroma 召回证据候选；启用 Rerank 后，再按“查询 + 候选正文”重排并选出最终证据。
6. 匹配、简历改写和回复草稿同时读取 SQLite 事实、上传简历正文与 RAG 证据。
7. 模型输出经过事实边界检查；不安全时回退到规则版草稿。
8. Agent、Embedding 和 Rerank 的供应商 Token usage 按账号写入 `usage_events`，供后台统计和后续计费。

### 存储边界

SQLite 是结构化事实源，适合精确过滤和比较：

- 学历。
- 工作和项目经验年限。
- 技能与熟练度。
- 证书和资格。
- 城市、薪资、工作形式和不可接受条件。
- 职位标准化字段。
- 账号、Session、候选人档案、对话和 Token 用量。
- 简历文件版本、文件哈希、解析方式、源文件与定制文件关系。

Chroma 是派生的语义检索索引，适合长文本召回：

- 项目描述和成果材料。
- 本地项目分析摘要。
- 职位职责和任职要求全文。
- 简历片段和自我介绍。
- 上传简历中提取的正文。
- HR 对话上下文。

向量库不是唯一事实源。结构化字段和候选人确认状态始终以 SQLite 为准。

## 目录结构

```text
Job-hunting Agent/
├─ src/
│  └─ job_hunting_agent/
│     ├─ agent.py                 # 标准 LangChain Agent 与工具注册
│     ├─ app.py                   # 业务应用门面
│     ├─ auth.py                  # 注册、登录和 Session
│     ├─ cli.py                   # CLI 入口
│     ├─ config.py                # .env 和运行配置
│     ├─ conversation_ingestion.py# 对话内容分类与自动入库
│     ├─ conversation_memory.py   # 持久化记忆和上下文压缩
│     ├─ job_parser.py            # BOSS 职位文本解析与校验
│     ├─ llm.py                   # LangChain ChatModel 适配
│     ├─ matcher.py               # 职位匹配、淘汰和排序规则
│     ├─ model_gateway.py         # 统一模型调用、用量和调用 ID
│     ├─ models.py                # 数据模型和领域对象
│     ├─ project_analyzer.py      # 本地项目最小必要读取与分析
│     ├─ rag.py                   # LangChain + Chroma RAG
│     ├─ resume_document.py       # DOCX/PDF 解析、扫描 PDF OCR 和受控文件存储
│     ├─ resume_exporter.py       # 职位定制 DOCX/PDF 导出
│     ├─ resume_writer.py         # 证据约束的简历草稿
│     ├─ storage.py               # SQLite 持久化和查询
│     ├─ web.py                   # FastAPI API 和 SSE
│     └─ web_static/
│        ├─ index.html            # Vue 3 页面结构
│        ├─ app.js                # 前端状态、请求和流式聊天
│        ├─ china_cities.js       # 中国省份和城市二级选择数据
│        ├─ styles.css            # 页面样式
│        ├─ tokens.css            # 前端设计 Token
│        └─ vendor/vue.global.prod.js
├─ tests/                         # 单元测试和 Web/API 回归测试
├─ docs/
│  ├─ adr/                        # 架构决策记录
│  ├─ learning/                   # 面向初学者的技术栈与操作说明
│  ├─ research/                   # BOSS 接入和职位标准化研究
│  └─ enterprise-readiness-decision-map.md # 企业级演进顺序与验收门槛
├─ CONTEXT.md                     # 领域术语和边界
├─ DECISION_MAP.md                # 项目决策地图
├─ pyproject.toml                 # 依赖和命令入口
├─ Dockerfile                    # Python Web 运行镜像构建步骤
├─ compose.yaml                  # Docker Compose 本地开发服务
├─ .dockerignore                 # Docker 构建上下文排除规则
├─ .env.example                   # 模型配置模板
└─ .gitignore                     # 密钥、数据库、缓存和运行数据忽略规则
```

`data/` 不提交到代码仓库。首次运行 Web 或 CLI 时会自动生成 SQLite 数据库、
Chroma 索引和简历文件目录。

## 核心文件说明

### 项目入口和配置

- `pyproject.toml`：声明 Python 版本、LangChain/FastAPI/Chroma/Argon2id 依赖，以及 `job-agent` 和 `job-agent-web` 命令。
- `.env.example`：展示模型、Embedding、记忆和 Cookie 配置项；真实 `.env` 不提交。
- `src/job_hunting_agent/config.py`：读取并校验环境变量，避免 API Key 写死在代码中。
- `src/job_hunting_agent/cli.py`：提供数据库初始化、创建管理员、创建档案、职位导入、RAG 重建和 Agent 对话命令。
- `src/job_hunting_agent/web.py`：提供认证、候选人档案、会话、职位、匹配、简历上传/下载、管理员和 SSE 聊天 API。

### 业务核心

- `agent.py`：创建标准 LangChain Agent，注册档案、项目、职位、简历文件和 HR 回复工具。
- `app.py`：组合存储、匹配、RAG、简历和对话服务，作为业务层门面。
- `models.py`：定义候选人档案、职位、匹配结果、简历草稿、对话和 Token 用量模型。
- `matcher.py`：执行学历硬门槛、经验差距淘汰、不可接受条件淘汰、技能匹配和普通偏好排序。
- `job_parser.py`：审核职位文本是否确实包含职位信息，并提取标准化字段。
- `project_analyzer.py`：只读取候选人指定项目中的必要文件，跳过密钥、缓存、依赖目录、构建产物和大型数据。
- `resume_document.py`：校验并解析 DOCX、文字 PDF 和扫描 PDF；文件只能写入受控目录。
- `resume_exporter.py`：把通过真实性检查的职位定制草稿导出为 DOCX 和 PDF。
- `resume_writer.py`：根据目标职位和上传简历生成证据约束草稿，保持候选人档案不被覆盖。

### 数据、记忆和模型

- `storage.py`：管理 SQLite 表、账号隔离、候选人档案、聊天消息、职位、简历文件版本和 Token 用量。
- `rag.py`：负责文本切分、Embedding、Chroma 写入、账号过滤、增量索引和可选 Rerank 重排。
- `conversation_ingestion.py`：判断当前对话内容应保存为结构化事实、长文本材料、项目确认项或普通聊天。
- `conversation_memory.py`：从 SQLite 恢复会话，超过阈值时压缩旧消息并保留最近上下文。
- `model_gateway.py`：为聊天、Embedding 和 Rerank 调用统一创建配置、有限重试、`call_id` 和不含正文的 Token 用量流水。
- `llm.py`：封装 LangChain ChatModel 与 OpenAI-compatible 接口细节。
- `auth.py`：实现 Argon2id 密码哈希、Session 滑动过期、最长有效期和退出所有设备。

### 前端和测试

- `web_static/index.html`：登录页、候选人工作台、职位面板和管理员后台的 Vue 模板。
- `web_static/app.js`：前端状态管理、SSE 消费、Markdown 渲染、档案、职位和简历文件操作。
- `web_static/styles.css`、`web_static/tokens.css`：布局、响应式规则和设计 Token。
- `tests/`：验证 Agent 工具链、认证、RAG、Embedding、简历解析/OCR/导出、项目分析和 Web 行为。

## 快速开始

### 1. 安装依赖

在项目根目录执行：

```powershell
python -m pip install -e .
```

### 2. 配置模型

复制 `.env.example` 为 `.env`，填写真实模型配置：

```dotenv
JOB_AGENT_LLM_PROVIDER=your-chat-provider
JOB_AGENT_LLM_MODEL=your-chat-model
JOB_AGENT_LLM_API_KEY=your-api-key
JOB_AGENT_LLM_BASE_URL=https://api.example.com/v1
JOB_AGENT_ENVIRONMENT=development
JOB_AGENT_MODEL_GATEWAY_CHAT_MAX_RETRIES=2
JOB_AGENT_MODEL_GATEWAY_EMBEDDING_MAX_RETRIES=2
JOB_AGENT_MODEL_GATEWAY_RERANK_MAX_RETRIES=2

JOB_AGENT_EMBEDDING_PROVIDER=local_hash
JOB_AGENT_EMBEDDING_MODEL=local-hash

JOB_AGENT_MEMORY_ENABLED=true
JOB_AGENT_COOKIE_SECURE=false
```

聊天模型、Embedding 模型和 Rerank 模型可以来自不同供应商。需要真实语义 Embedding 时，按
`.env.example` 中的通用协议配置替换本地模式；部署到 HTTPS 后，把 `JOB_AGENT_COOKIE_SECURE` 改为 `true`。

Embedding 支持以下协议样式：

| `JOB_AGENT_EMBEDDING_API_STYLE` | 请求/响应约定 |
| --- | --- |
| `openai_compatible` | `POST {base_url}/embeddings`，读取 `data[].embedding` |
| `native_multimodal` | 发送 `input.contents`，读取 `output.embeddings` |
| `local_hash` | 本地离线 fallback，不访问网络 |

Rerank 支持以下协议样式：

| `JOB_AGENT_RERANK_API_STYLE` | 请求/响应约定 |
| --- | --- |
| `standard` | `POST {base_url}/rerank`，发送 `query/documents`，读取 `results[]` |
| `native` | 发送 `input.query/documents` 与 `parameters`，读取 `output.results` |

Rerank 没有跨供应商统一标准。若目标服务使用不同字段或响应结构，应在 `rag.py` 中新增独立协议适配器，
而不是把供应商名称写进业务层。

启动前可确认配置已经被识别，命令只显示脱敏摘要：

```powershell
python -m job_hunting_agent.cli --env-file .env embedding-config
python -m job_hunting_agent.cli --env-file .env rerank-config
```

更换 Embedding 模型后，必须停止网页服务并对所有账号做一次全量 RAG 重建。Chroma 的一个集合
不能混存不同维度或不同语义空间的向量；仅做增量索引会留下旧向量，导致查询失败或结果失真。

```powershell
python -m job_hunting_agent.cli `
  --db data/job_agent.db `
  --env-file .env `
  --rag-dir data/chroma `
  rag-rebuild
```

Rerank 不参与建库，因此以后只更换 Rerank 模型时不需要重建 Chroma 索引。

### 3. 启动网页

```powershell
python -m job_hunting_agent.web `
  --db data/job_agent.db `
  --env-file .env `
  --rag-dir data/chroma `
  --resume-dir data/resumes
```

浏览器访问：

```text
http://127.0.0.1:8000
```

首次进入页面时注册普通账号。管理员账号不开放网页注册，可在服务停止时创建：

```powershell
python -m job_hunting_agent.cli `
  --db data/job_agent.db `
  create-admin
```

该命令会直接回显管理员密码，便于核对输入，请只在私密终端中使用；网页端登录仍会以星号隐藏密码。

### 4. 停止服务

在启动服务的终端按 `Ctrl+C`。如果服务由后台进程启动，需要结束对应的
`python -m job_hunting_agent.web` 进程。

### 5. 使用 Docker Compose 启动

如果不想在本机安装 Python 依赖，可以使用项目提供的 Docker 开发环境。第一次使用时，
确保项目根目录存在真实 `.env`；`.env` 只会以只读文件挂载到容器，不会打进镜像。

```powershell
docker compose build
docker compose up -d
docker compose ps
```

浏览器仍然访问 `http://127.0.0.1:8000`。查看日志、停止和删除容器：

```powershell
docker compose logs -f web
docker compose stop
docker compose down
```

`data/` 是宿主机绑定目录，保存 SQLite、Chroma、上传简历和导出文件；`down` 不会删除
这些数据。完整的 Docker 技术栈解释、首次启动步骤和后续扩展顺序见
[Docker 本地开发环境学习说明](./docs/learning/docker-environment.md)。

当前 Compose 只包含 `web` 服务，与本地 SQLite + Chroma 实现对应。PostgreSQL、pgvector、
Redis、Worker、MinIO 和反向代理会在完成相应代码和迁移后再加入。

## 常用功能

### 候选人档案

一个账号可以创建多个候选人档案，每个档案可以创建多个独立求职会话。账号是共享访问和统一 Token 计费主体，档案用于区分不同人的求职事实和上下文。

### 项目分析

可以把本地项目目录提供给 Agent。系统会优先读取 README、文档、依赖配置、源码入口、测试和部署配置，跳过以下内容：

- `.env`、密钥、账号配置和证书。
- 数据库文件、大型数据集和二进制产物。
- `node_modules`、虚拟环境、缓存和构建目录。
- 日志、临时文件和版本控制目录。

项目分析只生成待确认项目经历卡片。候选人确认前，技术栈、职责和成果不能作为已确认简历事实。

### 职位导入和匹配

把 BOSS 职位详情页中可见的文本复制到职位导入框。系统会先审核文本是否为职位信息，再进行保存和匹配。

匹配规则包括：

- 学历按硬性条件处理，候选人学历不能低于职位要求。
- 实际经验与职位要求相差超过 3 年时直接淘汰。
- 候选人明确不可接受的条件直接淘汰。
- 普通偏好只影响分数和排序。
- 技能熟练度只按候选人真实等级生成简历措辞，不自动夸大。

### 简历和 HR 回复

选择候选人档案后，在左侧“简历文件”区域上传 `.docx` 或 `.pdf`：

- DOCX 和文字 PDF 直接提取正文。
- 没有文本层的扫描 PDF 使用本地 RapidOCR 识别。
- 原文件、解析元数据和正文来源按账号与候选人保存；正文会增量登记到 RAG。
- 为原始简历选择已导入职位并点击“生成定制版”，系统会保存独立草稿，并生成可下载的 DOCX/PDF。
- 每次生成都是新版本，原始简历和候选人结构化档案不会被覆盖。
- 模型加入无证据技能、成果数字或擅自拔高熟练度时，改写会被丢弃并使用保守回退内容。

HR 回复只生成可编辑草稿，发送动作由候选人自行完成。

企业级 PostgreSQL、pgvector、Alembic、对象存储和容器化的实施顺序见
[企业级演进决策地图](./docs/enterprise-readiness-decision-map.md)。

## 准确性要求

1. 修改代码前必须阅读相关源码、现有测试和配置，不能只根据文件名猜测行为。
2. 修改 README 前必须检查当前目录结构、入口命令和实际 API，避免记录已经删除或不存在的内容。
3. 关键行为必须通过测试、静态检查或实际 HTTP 请求验证，不能只依赖“代码看起来正确”。
4. 代码、命令、文件名和环境变量发生变化时，必须同步检查 README、架构文档和测试中的旧引用。
5. 候选人技能、职责、成果数字和证书必须有来源；系统不能为了让简历更漂亮而补造事实。
6. 职位字段缺失或解析不确定时必须标记不确定，不能把猜测写成硬性条件。

## 内容要求

1. 使用普通 Markdown，不使用影响复制和检索的装饰性符号。
2. 内容准确、简洁，优先说明结论、边界和可执行命令。
3. 面向不同熟悉程度的读者，首次出现的技术术语要结合项目语境解释。
4. 命令必须使用代码块，路径和环境变量使用行内代码。
5. 说明“做什么、为什么这样做、在哪里修改”，避免只罗列文件名。
6. 不把测试数据、临时日志、截图和本机路径当成正式运行依赖。
7. 不在文档中写入 API Key、密码、Session、个人简历或职位隐私内容。
8. 对 BOSS 直聘的接入边界、候选人确认和人工发送动作必须明确说明。
9. 对 SQLite 事实源、RAG 派生索引和职位定制简历版本之间的关系必须保持一致。

## 结构要求

1. 文档按“项目概述 → 技术栈 → 项目架构 → 目录结构 → 核心文件 → 使用方法 → 准确性与内容约束”的顺序组织。
2. 每个主要模块只在一个位置解释，避免同一规则在多个段落出现不同版本。
3. 目录结构只展示源码、测试、文档和配置；运行时生成的 `data/` 内容不作为固定目录清单。
4. 所有相对链接都必须指向当前仍存在的文件。
5. 文档示例应能在全新工作区中按顺序执行，生成的数据和索引放入已忽略的 `data/` 目录。
6. 每次新增功能后，先更新对应核心文件说明，再更新快速开始或常用功能。

## 测试和检查

运行完整测试：

```powershell
python -m pytest -q
```

检查 Python 语法：

```powershell
python -m compileall src tests
```

运行前端快捷键和 SSE 流式超时回归检查（需要 Node.js）：

```powershell
node tests/frontend_shortcut_regression.mjs
node tests/frontend_stream_timeout_regression.mjs
```

检查 Git 差异中的空白错误：

```powershell
git diff --check
```

当前测试不依赖仓库中的数据库或向量索引。测试会使用临时目录，因此可以在清空
`data/` 后重新运行。
