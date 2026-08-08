# Docker 与 PostgreSQL 本地开发环境

## 这次改进解决什么问题

以前网页服务把结构化数据写入本地文件。现在这些数据只包含
测试内容，所以没有设计旧数据导入，而是直接把实际 Web 服务切换到 PostgreSQL。这样账号、
候选人档案、会话、职位、简历元数据和 Token 用量能使用外键、约束、事务和版本化迁移。

Docker Compose 现在不只是启动一个 Web 容器，而是按下面的顺序启动：

```text
postgres healthy
      |
      v
migrate runs Alembic upgrade
      |
      v
web starts FastAPI + Vue
```

Web 启动时只验证 Alembic revision，不会自行执行 `CREATE TABLE`。这让数据库结构变化
始终有一个可追踪、可回退的版本记录。

## 本次使用的技术栈

| 技术 | 在项目中的作用 | 为什么现在选用 |
| --- | --- | --- |
| Docker | 封装 Python、OCR/PDF 依赖和启动命令 | 不同电脑可以使用相同运行环境 |
| Dockerfile | 构建同时含应用代码和 Alembic 脚本的镜像 | Web 与迁移使用同一版本，避免代码和表结构错配 |
| Docker Compose | 声明 `postgres`、`migrate`、`web` 的网络、卷和依赖顺序 | 比手工启动三个进程更可复现，适合当前单机开发 |
| PostgreSQL 16 | 保存账号、档案、会话、职位、文件元数据和用量账本 | 支持事务、外键、JSONB、严格约束和生产级运维能力 |
| pgvector | 提供 PostgreSQL 的 `vector` 列类型和余弦距离查询 | RAG 分块、账号过滤和结构化事实可在同一个事务型数据库中管理 |
| SQLAlchemy 2.x | 创建 Engine、连接池和 PostgreSQL 执行边界 | 业务层不直接绑定 psycopg 驱动 |
| Alembic | 管理 `20260807_0001` 等数据库版本 | 新增字段、索引或表时能升级、审计和回退 |
| Psycopg 3 | SQLAlchemy 连接 PostgreSQL 的驱动 | SQLAlchemy 2.x 官方支持良好，支持 PostgreSQL 类型 |
| Docker named volume | 保存 PostgreSQL 数据目录 | 容器重建后数据库不会消失 |
| Bind mount | 把宿主机 `data/` 映射到容器 | 上传简历和导出文件可在容器重建后保留 |
| Healthcheck | 检查 PostgreSQL 可连接和 Web `/api/health` 可响应 | 区分“进程已启动”和“服务确实可用” |

## 当前数据边界

- PostgreSQL 是结构化事实源，也是 Web 服务实际使用的数据库。
- `data/` 只保存上传简历和导出文件，不再作为数据库或向量索引位置。
- 自动化测试也使用隔离的 PostgreSQL schema；网页和 Docker 运行入口不会创建本地数据库文件。
- PostgreSQL 的 `rag_chunks.embedding` 已由 pgvector 实际读写；检索先在数据库内按账号过滤，
  再按余弦距离排序。pgvector 是唯一的向量运行后端，网页不会创建独立向量目录。

## 第一次启动

确认项目根目录存在真实 `.env`。如果是从 GitHub 新下载的项目，先复制模板并填写模型配置：

```powershell
Copy-Item .env.example .env
```

默认使用 Docker Hub 镜像：

```powershell
docker compose up -d --build
docker compose ps
```

网络无法访问 Docker Hub 时，可以只在当前 PowerShell 会话使用镜像镜像源：

```powershell
$env:JOB_AGENT_DOCKER_BASE_IMAGE = "dockerproxy.net/library/python:3.12-slim"
$env:JOB_AGENT_POSTGRES_IMAGE = "dockerproxy.net/pgvector/pgvector:pg16"
docker compose up -d --build
Remove-Item Env:JOB_AGENT_DOCKER_BASE_IMAGE
Remove-Item Env:JOB_AGENT_POSTGRES_IMAGE
```

`postgres` 应显示 `healthy`，`migrate` 应显示 `Exited (0)`，`web` 应显示 `healthy`。
迁移日志可以确认 revision：

```powershell
docker compose logs migrate
```

浏览器访问：

```text
http://127.0.0.1:8000
```

## 本机直接运行 Python

如果不使用 Web 容器，而是在宿主机运行 `python -m job_hunting_agent.web`，先启动数据库：

```powershell
docker compose up -d postgres
```

在 `.env` 配置本机连接地址，再执行迁移。该变量是网页运行时必填项，缺失时服务会拒绝启动：

```dotenv
JOB_AGENT_DATABASE_URL=postgresql+psycopg://job_agent@127.0.0.1:5432/job_agent
```

```powershell
alembic upgrade head
python -m job_hunting_agent.web --env-file .env
```

Docker 中的 Web 不使用这个 `127.0.0.1` 地址，Compose 会将其覆盖为 `postgres` 服务名。

## 常用操作

```powershell
# 查看 PostgreSQL、迁移和网页日志
docker compose logs -f postgres
docker compose logs migrate
docker compose logs -f web

# 停止容器，但保留 PostgreSQL volume 和 data/ 文件
docker compose stop

# 删除容器与网络，仍保留 PostgreSQL volume 和 data/ 文件
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

`down -v` 会删除 `postgres_data`，其中也包含生产 RAG 向量；不会删除宿主机 `data/` 中的
简历文件。存在需要保留的数据库数据或简历文件时，不应执行此命令。

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
- `JOB_AGENT_DOCKER_BASE_IMAGE` 和 `JOB_AGENT_POSTGRES_IMAGE` 仅解决镜像下载问题，不应作为生产固定依赖。
- Redis、Worker、对象存储、备份、监控和高可用仍未实施，不能把当前 Compose 当作生产部署方案。
