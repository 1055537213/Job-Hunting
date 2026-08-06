# Docker 本地开发环境学习说明

## 这次改进解决什么问题

之前启动项目依赖本机的 Python、虚拟环境和已经安装的第三方包。换一台电脑时，
即使源码相同，也可能因为 Python 版本、系统库或依赖版本不同而启动失败。

这次把当前“SQLite + Chroma + 本地文件目录”的开发版封装为一个可重复启动的
Docker 服务。容器删除不会删除宿主机 `data/` 中的测试数据。

## 本次使用的技术栈

| 技术 | 作用 | 为什么现在选用 |
| --- | --- | --- |
| Docker | 把 Python、依赖和启动命令封装成镜像 | 让项目在不同电脑上拥有一致的运行环境 |
| Dockerfile | 描述镜像如何一步步构建 | 构建过程可审查、可复现，适合学习容器基础 |
| Docker Compose | 用 YAML 声明服务、端口、挂载和健康检查 | 当前只有一个 Web 容器，但以后可以自然扩展 PostgreSQL、Redis 和 Worker |
| `python:3.12-slim` | Python 运行时基础镜像 | 与 `pyproject.toml` 的 Python 要求一致，同时比完整系统镜像更小 |
| Bind mount | 把宿主机 `data/` 映射到容器 `/app/data` | SQLite、Chroma 和简历文件需要在容器重建后继续保留 |
| Healthcheck | 定期请求 `/api/health` | “进程启动了”和“服务真的能响应”是两件事，健康检查能区分它们 |
| `.dockerignore` | 排除密钥、数据库、缓存和 Git 文件 | 降低构建上下文大小，避免把敏感数据复制进镜像 |

## 文件之间的关系

```text
compose.yaml
    | 读取 Dockerfile、挂载 .env 和 data/
    v
Dockerfile
    | 构建 Python 3.12 + 项目依赖
    v
job-agent-web
    | 监听容器 8000 端口
    v
宿主机 http://127.0.0.1:8000

宿主机 data/ <----> 容器 /app/data
  SQLite / Chroma / resumes
```

## 第一次启动

确认项目根目录有 `.env`。如果是从 GitHub 新下载的项目，先复制模板并填写模型配置：

```powershell
Copy-Item .env.example .env
```

然后构建并后台启动：

```powershell
docker compose build
docker compose up -d
```

查看容器状态：

```powershell
docker compose ps
```

当 `web` 的状态显示 `healthy` 后，在浏览器打开：

```text
http://127.0.0.1:8000
```

## 常用操作

```powershell
# 查看实时日志
docker compose logs -f web

# 修改 Python 代码或依赖后重新构建并启动
docker compose up -d --build

# 停止服务，但保留 data/ 中的数据库、向量索引和简历文件
docker compose stop

# 停止并删除容器；由于 data/ 是绑定挂载，宿主机数据仍会保留
docker compose down

# 查看健康检查返回值
Invoke-WebRequest http://127.0.0.1:8000/api/health | Select-Object -ExpandProperty Content
```

## 现在为什么只有一个 `web` 服务

当前项目本地开发仍使用 SQLite、Chroma 和本地文件目录。把 PostgreSQL、pgvector、
Redis、MinIO 和 Worker 现在全部加入 Compose，会让你同时面对数据库迁移、对象存储、
任务队列和网络调试，学习成本很高，而且这些组件此刻还没有对应的业务代码。

后续会按这个顺序扩展：

1. 先完成 SQLAlchemy/Alembic，让数据结构可以安全迁移。
2. 再加入 PostgreSQL + pgvector，并保留 SQLite 本地适配器。
3. 然后加入 Redis 和 Worker，把 OCR、Embedding 与简历导出移出 Web 请求。
4. 最后加入 MinIO/S3-compatible 对象存储和反向代理。

## 重要边界

- `docker compose down` 不等于删除业务数据；`data/` 仍在宿主机。
- 删除或移动 `data/` 会丢失当前本地 SQLite、Chroma 和上传文件，操作前要备份。
- `.env` 只挂载到容器，不会复制进镜像；不要把真实 API Key 写入 `compose.yaml`。
- 这是本地开发环境，不代表已经具备企业生产部署所需的 HTTPS、备份、监控和高可用。
