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
| 关系数据库 | PostgreSQL + pgvector Compose 开发库和隔离测试 schema | PostgreSQL | Alembic 管理版本，不在请求启动时临时改生产表 |
| 向量检索 | PostgreSQL + pgvector | PostgreSQL + pgvector | 测试数据可丢弃，真实旧数据才需要回填校验 |
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
- 已完成 Web 请求级 Redis 分布式限流，以及 Chat、Embedding、Rerank、截图处理的
  Redis 全局/单账号共享并发租约；Chat、Embedding、Rerank 均已补齐超时、有限重试和
  进程内熔断，熔断状态会出现在健康检查中；分布式 trace 和集中故障告警仍留待后续。

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
已迁移到 Gateway；模型与截图并发已由 Redis 租约跨 Web/Worker 副本共享；Chat、Embedding
和 Rerank 的超时、有限重试、瞬时故障分类和进程内熔断也已统一实现。

### 阶段 2：PostgreSQL、pgvector 与 Alembic

目标：建立可审计、可回滚的生产数据迁移机制。

- 引入 SQLAlchemy 2.x 数据访问层和 Alembic。
- 创建 PostgreSQL schema，保留 `account_id`、外键、唯一约束和必要索引。
- 测试也使用 PostgreSQL 隔离 schema，不用另一种数据库语义污染领域接口。
- 将稳定 chunk ID、chunk metadata 和向量写入 pgvector。
- 若存在真实旧数据，编写一次性旧版本数据到当前 PostgreSQL schema 的导入和校验脚本。

当前状态：已完成 SQLAlchemy Engine/事务边界、冻结初始 Alembic revision、PostgreSQL +
pgvector Compose 启动链、Web 对 PostgreSQL 的结构化读写，以及 pgvector 的全量重建、增量
upsert、账号隔离检索、删除级联和真实 PostgreSQL 回归。当前测试数据允许丢弃，因此不实施
旧数据库或独立向量目录的数据导入。

完成门槛：空库可由 Alembic 升级到最新；生产启动不执行 `CREATE TABLE IF NOT EXISTS`；
RAG 在数据库内按账号隔离检索，重复增量索引不产生重复 chunk，删除事实源会级联删除向量。

### 阶段 3：对象存储与后台任务

目标：让 Web 请求不再承担 OCR、Embedding 和文档导出的长耗时工作。

- 用 S3-compatible 接口保存原始简历、项目压缩包和生成文件。
- Redis 作为任务队列基础设施；Worker 执行 OCR、索引、项目分析和批量导出。
- 任务记录包含 queued/running/succeeded/failed/cancelled 状态、进度、重试次数和错误摘要。
- 上传采用大小限制和文件签名检查；精确内容指纹去重已覆盖候选人档案、职位、同一候选人的
  项目卡片和原始简历；恶意文件扫描仍需补齐。
- 下载使用短期签名 URL 或经过鉴权的流式代理，不公开对象存储永久地址。

实施状态：第 3.1 步已完成。Docker Compose 已提供 MinIO，业务层通过 S3-compatible
对象存储接口写入原始简历和职位定制文件；PostgreSQL 继续保存归属、对象键、哈希和版本。
下载仍由鉴权后的 Web 流式代理返回，不公开对象存储地址。第 3.2 步已完成基础设施切片：
Compose 提供带密码的 Redis、Celery Worker 和 `background_tasks` 状态表；管理员探针已验证
Web -> PostgreSQL -> Redis -> Worker -> PostgreSQL 的完整链路。第 3.3 步已完成简历上传后的
RAG 增量 Embedding 迁移：Web 先保存文件和 `long_texts`，再登记 `rag_index` 任务。第 3.4 步
已完成扫描/混合 PDF OCR 迁移：Web 仅检查文本层并保存待处理原件，`resume_ocr` Worker 写入
正文和 `long_texts` 后自动创建 `rag_index`；Vue 按任务链轮询。第 3.5 步已完成公开 GitHub
项目分析迁移：Web/Agent 只接受规范化仓库首页 URL，Worker 通过官方 API/codeload 流式读取
受限归档，跳过敏感文件和不安全路径，生成待确认项目经历卡片；Vue 支持状态恢复和候选人确认，
确认后会创建独立 RAG 增量索引任务。GitHub ZIP 当前只在 Worker 内临时处理，尚未持久化到
对象存储。队列关闭时保留同步回退。项目分析的本地目录入口保留给未来桌面客户端；私有仓库
凭证和恶意文件扫描仍按垂直切片逐个实施；定制简历导出任务已迁移到 Worker，草稿和 DOCX/PDF
文件使用任务级幂等键，重试不会重复模型调用、扣费或生成版本。

当前已满足任务状态持久化、重复消息原子认领和失败投递恢复；完整阶段门槛仍包括恶意文件扫描
和 GitHub 归档持久化。文档导出异步化已完成，仍需上线前补充大文件、
高并发容量和 Worker 故障恢复演练。

### 阶段 4：安全、隐私与可观测性

目标：能够定位故障，同时避免日志成为新的隐私泄露点。

- 生产启用 HTTPS、Secure/HttpOnly/SameSite Cookie、CSRF 防护和安全响应头。
- 对登录、上传、模型调用和管理接口实施速率限制。
- 密钥进入 Secret 管理，不打包进镜像，不通过管理接口回显。
- 结构化日志只记录 ID、状态、耗时和脱敏错误；候选人正文默认不落日志。
- 建立请求指标、任务指标、模型调用指标、Token 成本指标和告警。
- 为管理员操作、账号禁用、文件访问和计费调整建立追加式审计日志。

实施状态：第 4.1 步已完成 Web 边缘硬化切片。FastAPI 统一安装请求硬化中间件，
为每个 HTTP 响应写入 `X-Request-ID`，输出不含请求正文的 JSON 访问日志，并附加
`Content-Security-Policy`、`X-Frame-Options`、`X-Content-Type-Options`、
`Referrer-Policy` 和 `Permissions-Policy`。已登录浏览器的 POST/PUT/DELETE
请求启用双提交 CSRF token，前端统一从 `job_agent_csrf` cookie 写入 `X-CSRF-Token`；
登录和普通请求也接入基础进程内限流。后续仍需补齐反向代理层限流、集中日志采集、指标、
告警、恶意文件扫描和管理员审计日志。

第 4.2 步已完成请求指标快照切片。Web 硬化中间件在进程内聚合总请求数、状态码分组、
平均/最大耗时、正在处理的请求数、限流次数、CSRF 拦截次数、endpoint 低基数统计和最近
错误摘要；管理员可通过后台页面和 `/api/admin/observability/requests` 查看。指标不保存
请求正文、查询参数、候选人材料或聊天内容。管理员页面仍只展示当前响应副本的进程内快照；
跨副本趋势、容量和告警由 Prometheus 聚合。

第 4.3 步已完成后台请求观测面板。管理端顶部保留摘要指标，下面补充状态分布、请求方法、
endpoint 热点和最近错误列表，方便管理员在同一页面定位限流、CSRF 和 5xx 问题；仍然不
展示正文、查询参数或密钥。

第 4.4 步已完成管理员操作审计基础切片。新增追加式 `admin_audit_events` 表和管理端
“管理员审计”面板，记录账号状态变更、退出所有设备和系统探针投递等低敏操作，并保存
`request_id` 以便和访问日志、请求指标关联；审计详情只保存资源 ID、状态和计数，不保存
候选人正文、查询参数或密钥。

第 4.5 步已完成 Prometheus 请求指标切片。Web 通过内部 `/internal/metrics` 导出标准
Prometheus 文本指标，生产 Compose 每 15 秒采集并保留 15 天趋势；告警规则覆盖 Web 不可用、
5xx 比例、平均耗时、安全拦截和并发请求。Prometheus 通过 Docker DNS 把每个 Web 副本作为
独立 target 采集，告警按 job 聚合；Caddy 同样动态发现副本并轮询分发流量。`validate_multi_replica.ps1`
会实际验证两个 Web 副本、跨副本 Redis 限流和两个健康采集目标。Caddy 明确拒绝公网
`/internal/*`，Prometheus 页面只绑定服务器回环地址；仍需在确定值班渠道后接入 Alertmanager。

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
- RAG：向量结果带账号过滤和来源 ID，能回溯到 PostgreSQL `long_texts` 登记材料；换模型、改切片或上线前必须通过黄金用例评测。
- 安全：密码仍使用 Argon2id；Session 闲置 7 天、最长 30 天、支持退出所有设备。
- 计费：只有供应商确认用量进入账单，幂等重试不会重复计费。
- 运维：健康检查、日志、指标、告警、备份和回滚都有可执行手册。
- 产品：所有简历与 HR 回复仍需候选人确认，系统不自动执行 BOSS 外部操作。

## 7. 当前恢复状态

- 已有：账号/Session、候选人多档案、多会话、Token 用量流水、LangChain Agent、RAG、职位匹配、内部 Model Gateway，以及可选 provider-native Embedding/Rerank。
- 本轮恢复：DOCX/PDF 上传、文字层解析、扫描 PDF OCR、简历文件版本、职位定制 DOCX/PDF 和鉴权下载。
- 本轮新增：SQLAlchemy、Alembic、PostgreSQL + pgvector Compose 服务、冻结的生产 schema，
  `postgres -> migrate -> web/worker` 启动链，以及 MinIO/S3 对象存储边界。Web 的结构化业务
  读写已切换到 PostgreSQL，简历文件已切换到 MinIO named volume。
- 后台基础设施：Redis broker、Celery Worker、`background_tasks` 状态表、任务幂等键、
  进度/重试/错误摘要和管理员探针接口；扫描 PDF OCR、简历 RAG 增量 Embedding 和公开 GitHub
  项目分析和定制简历导出均已接入 Worker。当前还具备单机生产 Compose、HTTPS、备份恢复脚本、请求指标和管理员审计。
  支付记账基础已新增充值订单、低敏支付事件、幂等到账和管理员人工补款；人工补款不伪装成支付收入，
  且余额、流水和管理员审计同事务提交。Prometheus 已接入低敏请求指标、15 天趋势和告警规则。
  Caddy 与 Prometheus 均可动态发现多个 Web 副本，仓库提供可自动恢复单实例开发环境的
  双副本验收脚本。
  尚未实施的是 Alertmanager 通知与分布式 Trace、真实支付渠道签名回调、
  退款对账和高可用部署。

后续改造必须按上述阶段逐步提交。当前 pgvector RAG 已进入生产读写路径；测试和 Web 共用
PostgreSQL 后端。文件正文已不再依赖宿主机目录，请求限流和模型/截图并发已迁移到 Redis；
多副本指标采集和共享保护已完成验证；正式启用多副本前仍需迁移进程内会话记忆，并完成
更长时间的并发、滚动更新和故障演练。
