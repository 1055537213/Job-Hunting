# 求职助手企业级演进决策地图

本文记录从当前 MVP 演进到可上线产品时已经确认的架构结论、实施顺序和验收门槛。
它不是“把所有组件一次装齐”的清单；每一阶段都必须保持系统可运行、可回滚、可观测。

## 1. 已确认的产品与数据边界

- 一个账号可以拥有多份候选人档案，同一账号内允许共享职位池和使用额度。
- 每份候选人档案可以拥有多个独立会话，用于不同职位和不同求职目标。
- 不同账号必须按 `account_id` 隔离档案、会话、职位、RAG、简历文件和用量流水。
- 候选人结构化档案是事实源；简历改写结果保存为独立草稿和文件版本，不反向覆盖档案。
- BOSS 直聘继续采用用户介入的只读导入，不自动登录、爬取、投递或发送 HR 消息。
- 供应商确认的 Token 用量才进入正式可计费汇总；估算值和缺失值只能用于排障。

## 2. 已确认的技术方向

| 领域 | 本地开发/测试 | 线上生产 | 迁移策略 |
|---|---|---|---|
| 关系数据库 | SQLite | PostgreSQL | Alembic 管理版本，不在请求启动时临时改生产表 |
| 向量检索 | Chroma 可继续用于教学和离线测试 | PostgreSQL + pgvector | 先双写/回填校验，再切读路径 |
| 模型接入 | `.env` 配置 OpenAI-compatible 接口 | 内部 Model Gateway 模块 | 先保留在模块化单体内，达到拆分条件后再独立服务 |
| 文件存储 | 本地受控目录 | S3-compatible 对象存储 | 数据库只存对象键、哈希、版本和归属 |
| 本地编排 | Docker Compose | Docker Compose 可用于单机早期环境 | 多副本和弹性需求出现后再引入 Kubernetes |

MySQL 不作为当前生产主方案。它可以保存关系数据，但不能直接替代已确认的
PostgreSQL + pgvector 组合；改用 MySQL 会额外引入独立向量数据库和跨库一致性成本。

## 3. 目标形态

```text
Vue 3 Web
   |
   v
FastAPI 模块化单体
   |-- Auth / Account / Candidate / Conversation
   |-- Job ingestion / Matching
   |-- Resume document / OCR / Export
   |-- Internal Model Gateway
   |-- Usage ledger / Billing projection
   |
   +--> PostgreSQL + pgvector
   +--> S3-compatible object storage
   +--> Redis task queue/cache
   +--> Worker: OCR, embedding, resume export, long-running analysis
   +--> LLM / Embedding providers

Observability: structured logs + metrics + traces + error reporting
```

第一阶段仍采用模块化单体。认证、业务规则、用量计量和文件元数据共享一致事务边界，
过早拆成微服务只会增加网络失败、分布式追踪和数据一致性成本。

## 4. 内部 Model Gateway

Model Gateway 是模型供应商与业务代码之间的统一入口，不是新的聊天 Agent。

它负责：

- 根据 operation 选择聊天模型、Embedding 模型、Rerank 模型和后备模型。
- 统一超时、重试、并发限制、熔断和供应商错误映射。
- 统一请求 ID、调用 ID、供应商请求 ID 和 Token usage 采集。
- 对密钥和 Base URL 做集中配置，业务模块不直接读取供应商密钥。
- 保留模型、提示词版本和响应状态，支持成本审计和质量回归。
- 明确禁止把包含候选人隐私的原始请求写入普通日志。

当前先在 FastAPI 模块化单体中实现接口，例如：

```text
ModelGateway.chat(operation, messages, context)
ModelGateway.embed(operation, texts, context)
```

当前实现状态：

- 已完成：类型化运行环境配置、聊天模型、Embedding 与 Rerank 的统一工厂、有限重试、
  `root_request_id`/`call_id`、供应商 request ID 提取和不含正文的 usage 流水。
- 已迁移：LangChain Agent 主聊天、工具内单轮 LLM、网页端简历改写、RAG Embedding 和可选 Rerank。
- 后续补齐：并发限流、熔断、分布式 trace 和供应商故障告警；这些需要和 Redis、
  可观测性基础设施一起在后续阶段实施。

只有出现以下任一条件时才考虑拆成独立服务：多个产品共同使用、需要独立扩缩容、
供应商路由规则频繁变化，或模型调用故障需要与主业务进程隔离。

## 5. 分阶段实施

### 阶段 0：稳定当前恢复基线

目标：确保从 GitHub 基线恢复的功能可重复验证。

- 补齐上传简历、OCR、职位定制文件和下载链路。
- 保持 `.env`、数据库、RAG、上传文件和导出文件不进入 Git。
- 跑通聚焦测试与完整回归测试。
- 每个可独立回滚的恢复批次单独提交。

完成门槛：测试通过；账号越权下载被拒绝；原档案和原始简历不会被改写覆盖。

### 阶段 1：配置和应用边界标准化

目标：消除入口各自创建模型、拼接路径或直接访问存储的情况。

- 引入类型化 Settings，区分开发、测试和生产配置。
- 完成内部 Model Gateway 接口，迁移 Agent、简历改写、Embedding 和 Rerank 调用。
- 为每次上游调用生成幂等 `call_id`，统一记录 provider usage。
- 把文件目录访问收束到文件存储接口，禁止路由直接拼服务器路径。

完成门槛：业务层不依赖具体模型 SDK；切换 OpenAI-compatible 中转站只改配置。

实施状态：核心模型边界已完成。`model_gateway.py` 是当前模块化单体中的唯一模型
入口，`JOB_AGENT_ENVIRONMENT` 可显式选择 development/test/production，现有业务调用
已迁移到 Gateway；并发控制与熔断留待 Redis 和可观测性阶段实现。

### 阶段 2：PostgreSQL、pgvector 与 Alembic

目标：建立可审计、可回滚的生产数据迁移机制。

- 引入 SQLAlchemy 2.x 数据访问层和 Alembic。
- 创建 PostgreSQL schema，保留 `account_id`、外键、唯一约束和必要索引。
- 将 SQLite 作为本地适配器，不用 SQLite 特有语义污染领域接口。
- 将 Chroma 文档 ID、chunk metadata 和向量迁移到 pgvector。
- 编写 SQLite 到 PostgreSQL 的一次性导入和校验脚本。

完成门槛：空库可由 Alembic 升级到最新；生产启动不执行 `CREATE TABLE IF NOT EXISTS`；
回填后记录数、归属关系、向量检索样本和哈希校验一致。

### 阶段 3：对象存储与后台任务

目标：让 Web 请求不再承担 OCR、Embedding 和文档导出的长耗时工作。

- 用 S3-compatible 接口保存原始简历、项目压缩包和生成文件。
- Redis 作为任务队列基础设施；Worker 执行 OCR、索引、项目分析和批量导出。
- 任务记录包含 queued/running/succeeded/failed/cancelled 状态、进度、重试次数和错误摘要。
- 上传采用大小限制、文件签名检查、哈希去重和恶意文件扫描。
- 下载使用短期签名 URL 或经过鉴权的流式代理，不公开对象存储永久地址。

完成门槛：Web 进程重启不丢任务；重复提交不会生成重复账单或重复文件版本；失败可重试。

### 阶段 4：安全、隐私与可观测性

目标：能够定位故障，同时避免日志成为新的隐私泄露点。

- 生产启用 HTTPS、Secure/HttpOnly/SameSite Cookie、CSRF 防护和安全响应头。
- 对登录、上传、模型调用和管理接口实施速率限制。
- 密钥进入 Secret 管理，不打包进镜像，不通过管理接口回显。
- 结构化日志只记录 ID、状态、耗时和脱敏错误；候选人正文默认不落日志。
- 建立请求指标、任务指标、模型调用指标、Token 成本指标和告警。
- 为管理员操作、账号禁用、文件访问和计费调整建立追加式审计日志。

完成门槛：可以从 request ID 追踪一次请求到任务、模型调用和用量流水；安全扫描无高危项。

### 阶段 5：计费与额度

目标：把已有用量流水升级为可对账的计费基础，而不是直接按页面显示数字收费。

- 建立不可变 usage ledger、供应商价格版本和账期快照。
- 区分输入、输出、缓存、推理、Embedding 和 Rerank Token；保留供应商原始 usage 摘要。
- 增加账号额度、预警阈值、并发限制和超额处理策略。
- 重试只对真实成功的上游调用计费，使用 `call_id` 保证幂等。
- 账单汇总必须能反查到调用流水，但不能向管理员暴露候选人正文。

完成门槛：抽样账单可与供应商账单对齐；缺失 usage 不进入可计费 Token。

### 阶段 6：容器化发布

目标：先用一套可复现的单机生产拓扑上线小流量版本。

Docker Compose 初始服务建议：

- `web`：FastAPI API 与 Vue 静态资源。
- `worker`：OCR、Embedding、项目分析与导出任务。
- `postgres`：PostgreSQL + pgvector。
- `redis`：任务队列与短期缓存。
- `object-storage`：本地/测试使用 MinIO，云上可替换成托管 S3。
- `reverse-proxy`：TLS、请求大小限制和静态缓存策略。

完成门槛：新机器只依赖 Docker 和配置文件即可启动；数据库、对象和密钥均使用持久卷或外部托管服务。

### 阶段 7：按触发条件引入 Kubernetes

Kubernetes 不是上线前置条件。只有出现以下需求时再进入该阶段：

- Web/Worker 需要多节点自动扩缩容。
- 发布需要滚动更新、健康探针和自动回滚。
- 单机故障已不能满足可用性目标。
- 多环境配置和 Secret 数量使 Compose 难以维护。

引入后优先使用托管 PostgreSQL、Redis 和对象存储；Kubernetes 只承载无状态 Web/Worker，
避免第一版同时承担数据库高可用、存储编排和业务上线三类复杂度。

## 6. 生产验收清单

- 数据：所有读写都带账号归属；迁移、备份恢复和删除流程经过演练。
- 文件：扩展名、签名、大小、页数、哈希和下载权限均被校验。
- 模型：超时、重试、限流、usage 缺失和供应商故障均有明确状态。
- RAG：向量结果带账号过滤和来源 ID，能回溯到 SQLite/PostgreSQL 登记材料。
- 安全：密码仍使用 Argon2id；Session 闲置 7 天、最长 30 天、支持退出所有设备。
- 计费：只有供应商确认用量进入账单，幂等重试不会重复计费。
- 运维：健康检查、日志、指标、告警、备份和回滚都有可执行手册。
- 产品：所有简历与 HR 回复仍需候选人确认，系统不自动执行 BOSS 外部操作。

## 7. 当前恢复状态

- 已有：账号/Session、候选人多档案、多会话、Token 用量流水、LangChain Agent、RAG、职位匹配、内部 Model Gateway，以及可选 DashScope Embedding/Rerank。
- 本轮恢复：DOCX/PDF 上传、文字层解析、扫描 PDF OCR、简历文件版本、职位定制 DOCX/PDF 和鉴权下载。
- 尚未实施：SQLAlchemy/Alembic、PostgreSQL/pgvector、对象存储、任务队列、生产可观测性和容器编排文件。

后续改造必须按上述阶段逐步提交。不能在尚未建立迁移和回滚能力时，直接把本地 SQLite
数据路径替换成生产 PostgreSQL，也不能在文件仍依赖单机目录时先做多副本部署。
