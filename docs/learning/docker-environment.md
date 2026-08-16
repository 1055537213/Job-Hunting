# Docker、PostgreSQL、MinIO 与 Redis 本地开发环境

## 这次改进解决什么问题

以前网页服务把结构化数据写入本地文件。现在这些数据只包含
测试内容，所以没有设计旧数据导入，而是直接把实际 Web 服务切换到 PostgreSQL。这样账号、
候选人档案、会话、职位、简历元数据和 Token 用量能使用外键、约束、事务和版本化迁移。

Docker Compose 现在不只是启动一个 Web 容器，而是同时启动数据库、对象存储、Redis 和独立 Worker：

```text
postgres healthy -----> migrate runs Alembic upgrade --+
                                                         +--> web starts FastAPI + Vue
minio healthy ------------------------------------------+
redis healthy ------------------------------------------+--> worker starts Celery consumer
```

Web 启动时只验证 Alembic revision，不会自行执行 `CREATE TABLE`。这让数据库结构变化
始终有一个可追踪、可回退的版本记录。

## 本次使用的技术栈

| 技术 | 在项目中的作用 | 为什么现在选用 |
| --- | --- | --- |
| Docker | 封装 Python、OCR/PDF 依赖和启动命令 | 不同电脑可以使用相同运行环境 |
| Dockerfile | 构建同时含应用代码和 Alembic 脚本的镜像 | Web 与迁移使用同一版本，避免代码和表结构错配 |
| Docker Compose | 声明 `postgres`、`minio`、`redis`、`migrate`、`web`、`worker` 的网络、卷和依赖顺序 | 比手工启动多个进程更可复现，适合当前单机开发 |
| PostgreSQL 16 | 保存账号、档案、会话、职位、文件元数据和用量账本 | 支持事务、外键、JSONB、严格约束和生产级运维能力 |
| pgvector | 提供 PostgreSQL 的 `vector` 列类型和余弦距离查询 | RAG 分块、账号过滤和结构化事实可在同一个事务型数据库中管理 |
| SQLAlchemy 2.x | 创建 Engine、连接池和 PostgreSQL 执行边界 | 业务层不直接绑定 psycopg 驱动 |
| Alembic | 管理 `20260807_0001` 等数据库版本 | 新增字段、索引或表时能升级、审计和回退 |
| Psycopg 3 | SQLAlchemy 连接 PostgreSQL 的驱动 | SQLAlchemy 2.x 官方支持良好，支持 PostgreSQL 类型 |
| MinIO | 在本地提供 S3-compatible 对象存储 API | 先按生产对象存储的接口开发，云上可替换为托管 S3 |
| boto3 | Python 通过标准 S3 API 读写 MinIO | 业务代码不绑定 MinIO 私有协议，后续替换云厂商只改配置 |
| Redis | 作为 Celery broker 传递短期任务消息 | Web 不阻塞，Worker 可单独扩容；不把事实数据放进缓存 |
| Celery | 管理任务确认、重试、超时和 Worker 消费 | 不手写 Redis 消息协议；当前已承载公开 GitHub 项目分析、扫描 PDF OCR 和简历 RAG 增量 Embedding |
| Docker named volume | 保存 PostgreSQL、MinIO 和 Redis AOF 数据目录 | 容器重建后数据库、文件正文和未确认队列消息尽量保留 |
| Healthcheck | 检查 PostgreSQL、MinIO、Redis 和 Web `/api/health` 可响应 | 区分“进程已启动”和“服务确实可用” |

## 依赖版本锁定

项目用 `pyproject.toml` 声明“允许使用的版本范围”，用 `requirements.lock` 保存一次经过解析的
精确版本。两者分工不同：前者方便升级和表达兼容范围，后者保证 Web、Worker 和 Alembic
迁移容器在不同时间构建时仍安装同一套依赖，避免 OCR、LangChain 或数据库驱动在无意中升级。

`requirements.lock` 由开发期工具 `pip-tools` 生成，不需要安装进生产镜像。修改
`pyproject.toml` 后，在项目根目录执行：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -m piptools compile pyproject.toml `
  --output-file requirements.lock --resolver=backtracking --strip-extras --no-emit-index-url
```

Dockerfile 会先执行 `pip install -r requirements.lock`，再以 `--no-deps` 安装本项目；不要
手工编辑锁文件。提交依赖变更时，应同时提交 `pyproject.toml` 和重新生成的锁文件。

## Python 3.12.13 运行时

项目将 Docker 和宿主机开发运行时统一到 Python 3.12.13。Dockerfile 和 Compose 默认使用
精确的 `python:3.12.13-slim` 标签，而不是会随上游更新的 `python:3.12-slim` 浮动标签。

宿主机开发继续使用现有的 `E:\Anaconda\envs\langchain1.2` Conda 环境，不需要替换或迁移
本地环境。确认解释器版本并安装项目依赖：

```powershell
E:\Anaconda\envs\langchain1.2\python.exe --version
E:\Anaconda\envs\langchain1.2\python.exe -m pip install -r requirements.lock
E:\Anaconda\envs\langchain1.2\python.exe -m pip install --no-deps -e .
```

## 当前数据边界

- PostgreSQL 是结构化事实源，也是 Web 服务实际使用的数据库。
- 上传简历和导出文件存入 MinIO；PostgreSQL 只保存对象键、哈希、媒体类型、归属和版本等元数据。
- 队列开启时，上传接口只做文本层检查、原件保存和事实登记：扫描 PDF 返回 `resume_ocr`
  任务，Worker 从 MinIO 读取原件完成 OCR 后写入 `long_texts`，再自动创建 `rag_index`。
  文字版文件直接创建 `rag_index`。所有状态写回 `background_tasks`，前端通过
  `/api/tasks/{task_key}` 轮询；队列关闭时仍同步解析，方便没有 Redis 的宿主机测试。
- Docker Web 不再挂载宿主机 `data/`；该目录只保留给显式本地测试适配器使用。网页项目分析读取
  用户主动提供的公开 GitHub 仓库，Worker 只连接 GitHub 官方 API/codeload，不访问用户电脑本地路径。
- 自动化测试也使用隔离的 PostgreSQL schema；网页和 Docker 运行入口不会创建本地数据库文件。
- PostgreSQL 的 `rag_chunks.embedding` 已由 pgvector 实际读写；检索先在数据库内按账号过滤，
  再按余弦距离排序。pgvector 是唯一的向量运行后端，网页不会创建独立向量目录。

## 第一次启动

确认项目根目录存在真实 `.env`。如果是从 GitHub 新下载的项目，先复制模板并填写模型配置：

```powershell
Copy-Item .env.example .env
```

默认使用 Docker Hub 镜像；依赖安装使用已提交的 `requirements.lock`：

```powershell
docker compose up -d --build
docker compose ps
```

网络无法访问 Docker Hub 时，可以只在当前 PowerShell 会话使用镜像镜像源：

```powershell
$env:JOB_AGENT_DOCKER_BASE_IMAGE = "dockerproxy.net/library/python:3.12.13-slim"
$env:JOB_AGENT_POSTGRES_IMAGE = "dockerproxy.net/pgvector/pgvector:pg16"
$env:JOB_AGENT_MINIO_IMAGE = "dockerproxy.net/minio/minio:RELEASE.2025-04-22T22-12-26Z"
$env:JOB_AGENT_REDIS_IMAGE = "dockerproxy.net/library/redis:7.4-alpine"
docker compose up -d --build
Remove-Item Env:JOB_AGENT_DOCKER_BASE_IMAGE
Remove-Item Env:JOB_AGENT_POSTGRES_IMAGE
Remove-Item Env:JOB_AGENT_MINIO_IMAGE
Remove-Item Env:JOB_AGENT_REDIS_IMAGE
```

`postgres`、`minio` 和 `redis` 应显示 `healthy`，`migrate` 应显示 `Exited (0)`，`web` 和 `worker` 应处于运行状态。
迁移日志可以确认 revision：

```powershell
docker compose logs migrate
docker compose logs worker
```

浏览器访问：

```text
http://127.0.0.1:8000
```

## 本机直接运行 Python

如果不使用 Web 容器，而是在宿主机运行 `python -m job_hunting_agent.web`，先启动数据库和 MinIO：

```powershell
docker compose up -d postgres minio
```

在 `.env` 配置本机连接地址，再执行迁移。该变量是网页运行时必填项，缺失时服务会拒绝启动：

```dotenv
JOB_AGENT_DATABASE_URL=postgresql+psycopg://job_agent@127.0.0.1:5432/job_agent
JOB_AGENT_OBJECT_STORAGE_BACKEND=s3
JOB_AGENT_OBJECT_STORAGE_ENDPOINT=http://127.0.0.1:9000
JOB_AGENT_OBJECT_STORAGE_BUCKET=job-agent-files
JOB_AGENT_OBJECT_STORAGE_ACCESS_KEY=replace-with-minio-access-key
JOB_AGENT_OBJECT_STORAGE_SECRET_KEY=replace-with-a-strong-minio-secret
JOB_AGENT_REDIS_PASSWORD=replace-with-a-strong-redis-password
```

```powershell
E:\Anaconda\envs\langchain1.2\python.exe -m alembic upgrade head
E:\Anaconda\envs\langchain1.2\python.exe -m job_hunting_agent.web --env-file .env
```

Docker 中的 Web 不使用这个 `127.0.0.1` 地址，Compose 会将其覆盖为 `postgres` 服务名。

## 常用操作

```powershell
# 查看 PostgreSQL、MinIO、Redis、迁移、网页和 Worker 日志
docker compose logs -f postgres
docker compose logs -f minio
docker compose logs -f redis
docker compose logs migrate
docker compose logs -f web
docker compose logs -f worker

# 停止容器，但保留 PostgreSQL、MinIO 和 Redis volume
docker compose stop

# 删除容器与网络，仍保留 PostgreSQL、MinIO 和 Redis volume
docker compose down

# 查看 Web 健康检查
Invoke-WebRequest http://127.0.0.1:8000/api/health | Select-Object -ExpandProperty Content

# 仅查看当前 Alembic revision
alembic current
```

当前数据库数据是测试数据时，可重新创建 PostgreSQL：

```powershell
docker compose down -v
docker compose up -d
```

`down -v` 会删除 `postgres_data`、`minio_data` 和 `redis_data`：前者包含结构化数据与 RAG
向量，第二个包含原始和导出的简历文件，第三个包含 Redis AOF 队列数据。存在需要保留的
数据库、简历文件或未完成任务时，不应执行此命令。

## 开发模式：源码热更新

默认 `compose.yaml` 使用镜像中的固定源码，适合检查可复现部署。修改 Python、Alembic、
依赖或 Dockerfile 后需要重新构建：

```powershell
docker compose up -d --build
```

日常改动 `src/` 下的 Python、Vue JS 或 CSS 时，可以叠加本机开发配置：

```powershell
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

Python 文件变动会触发 Uvicorn 重载；前端静态文件刷新浏览器即可读取。`.env` 变化后仍需：

```powershell
docker compose -f compose.yaml -f compose.dev.yaml restart web
```

## 安全边界

- 当前 Compose 使用 `POSTGRES_HOST_AUTH_METHOD=trust`，只绑定 `127.0.0.1`，仅适合本机开发。
- 生产环境必须改用强密码、Secret 管理、私有网络、最小权限账号和 TLS。
- `.env` 仍只读挂载到容器，不会复制进镜像；不要把真实 API Key、数据库密码或 Session 写入 `compose.yaml`。
- `JOB_AGENT_DOCKER_BASE_IMAGE`、`JOB_AGENT_POSTGRES_IMAGE`、`JOB_AGENT_MINIO_IMAGE` 和
  `JOB_AGENT_REDIS_IMAGE` 仅解决镜像下载问题，不应作为生产固定依赖。
- 当前 Redis/Worker 已完成队列基础设施、系统探针、公开 GitHub 项目分析、扫描 PDF OCR 和简历 RAG 增量索引；
  尚未完成的是项目分析的本地目录客户端入口、简历导出迁移、备份、监控和高可用，因此不能把当前
  Compose 直接当作生产部署方案。
