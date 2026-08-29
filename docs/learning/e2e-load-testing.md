# 端到端负载与故障测试

这项验收覆盖真实网络和基础设施链路：

```text
HTTP / Cookie / CSRF
  -> FastAPI / Uvicorn
  -> PostgreSQL / pgvector
  -> Redis / Celery
  -> Worker
  -> PostgreSQL 任务状态
  -> HTTP 轮询
```

测试不会调用真实 LLM、Embedding 或 Reranker。聊天使用确定性流式替身，RAG 使用
`local_hash` 生成 2560 维测试向量，索引通过真实 Celery `rag_index` 任务完成；队列探针使用
不读取用户材料的 `system_probe`。
因此这里衡量的是系统链路、并发行为和故障恢复，不是模型质量或真实供应商延迟。

## 运行方式

先确认 `.env` 已包含本地 Docker Redis 的 `JOB_AGENT_REDIS_PASSWORD`，然后执行。脚本默认
使用 Redis DB 15，与项目业务 Celery 默认使用的 DB 0 分开。目标 DB 必须为空；如果已有键，
脚本会拒绝执行而不是清空未知数据。测试结束时会清空这个专用 DB：

```powershell
# 开发过程中的快速验收：1 / 5 并发
.\scripts\validate_e2e_load.ps1 `
  -Profile smoke `
  -Python E:\Anaconda\python.exe

# 上线前完整验收：1 / 5 / 10 / 20 / 50 并发
.\scripts\validate_e2e_load.ps1 `
  -Profile full `
  -RequestsPerUser 2 `
  -Python E:\Anaconda\python.exe
```

默认宿主机数据库为：

```text
postgresql+psycopg://job_agent@127.0.0.1:5432/job_agent
```

生产式密码认证或非默认端口可以显式传入 `-DatabaseUrl`、`-RedisUrl`。如果 Worker
容器不能通过 `postgres` 和 `redis` 服务名访问同一实例，再同时传入
`-WorkerDatabaseUrl` 和 `-WorkerRedisUrl`。连接串只进入临时环境文件，不会写进报告。

## 隔离和清理

每次运行都会：

1. 创建随机 PostgreSQL schema，并执行完整 Alembic migration。
2. 创建随机 Celery 队列和临时账号，不读写 `public` schema 中的正式账号。
3. 为每个虚拟用户建立独立 Cookie、CSRF token、候选人档案和 pgvector 证据。
4. 启动只消费本轮随机队列的临时 Worker 容器，并通过 `rag_index` 写入 pgvector。
5. 在 `finally` 中停止 Worker、清空专用 Redis DB、关闭连接并
   `DROP SCHEMA ... CASCADE`。

原始样本和汇总报告写入已被 Git 忽略的 `data/eval-reports/`。报告生成前会递归删除
密码、Cookie、CSRF token、API Key、数据库 URL 和 Redis URL 中的认证信息。

## 覆盖场景

- 健康检查、档案列表和单档案读取。
- 多租户 RAG 查询，必须返回当前账号的唯一测试证据。
- 多事件 SSE：状态、4 个 token、任务完成和 final 事件。
- 管理员提交 `system_probe`，Worker 完成后由网页轮询 PostgreSQL 状态。
- 缺少 CSRF 的写请求必须被拒绝。
- 账号 A 读取账号 B 的档案必须被拒绝。
- 完全重复的候选人档案必须返回 409。
- 模型熔断和超时必须成为 SSE `error` 事件，不能让连接无提示中断。
- Worker 停止期间任务保持 `queued`，专用 Worker 重启后必须成功消费。

## 默认门禁

- 普通 API 错误率小于 1%。
- 普通 API P95 小于 500ms。
- 包含鉴权、HTTP 和 2560 维 pgvector 检索的 RAG API P95 小于 1.5 秒。
- SSE 无协议错误或中断。
- SSE 首事件 P95 小于 2 秒。
- 后台任务不丢失、不失败、不超时。
- CSRF、租户隔离、重复写入和故障事件全部符合预期。

这些是单机开发环境的初始门禁，不应直接当作生产容量承诺。正式上线前应在与生产相近的
CPU、内存、网络、Worker 数量和 PostgreSQL 参数下执行 `full`，并根据业务 SLO 固化阈值。

## CI 边界

`tests/test_e2e_load.py` 会启动真实 Uvicorn TCP 端口，走注册、登录、Cookie、CSRF、档案和
多事件 SSE。它复用 CI 的隔离 PostgreSQL schema，不启动 Docker Worker，成本足够低，可以
随每次提交运行。完整 Redis/Celery 压测保留为手动发布验收，避免普通 CI 因嵌套容器、机器
性能波动或 50 并发产生不稳定结果。
