# Python + Java 服务边界

## 当前状态

现有线上环境仍使用独立的纯 Python 版本。本分支在本地 Docker 运行 Python+Java 混合版本：
Java 已负责账号生命周期、Session、权限、账务、已迁移的管理查询，以及档案和聊天的公开入口；
Python 负责 Agent、RAG、文件分析、档案/消息事实和后台任务。后文按阶段保留迁移历史，
各阶段“当时保留在 Python”的描述不代表当前仍未迁移。混合版尚未部署到生产服务器。

## 目标边界

| 模块 | 当前实现 | 目标所有者 |
| --- | --- | --- |
| Agent、RAG、OCR、视觉分析、模型网关 | Python | Python AI 服务 |
| AI 长任务 Worker | Python + Celery | Python AI 服务 |
| 账号、Session、权限、余额、账务 | Java，Python 保留兼容调用 | Java 平台服务 |
| 管理员查询与已迁移的操作入口 | Java，Python 保留 AI/任务事实写入 | 按事实所有权通过内部接口协作 |
| 档案与聊天公开入口、SSE 校验转发 | Java；Python 执行工具并存储事实 | Java 负责入口，Python 负责 AI 执行 |
| PostgreSQL、Redis、MinIO | 基础设施 | 共享基础设施，按表和接口隔离 |

## 迁移规则

1. 同一业务写入操作只能有一个服务负责执行；迁移完成后收束为一张事实表一个写入所有者。
2. 迁移前必须先冻结 OpenAPI 契约、错误码、幂等键和 trace id 规则。
3. Python 和 Java 不通过直接改对方表来通信。
4. 长耗时后台操作使用任务 ID；交互式聊天允许有界同步等待或流式转发，但不持有数据库事务等待模型。
5. 任何新写入路径必须先有集成测试，再切换生产流量。

## 第一阶段交付

- `platform-service/`：可构建的 Spring Boot 运行骨架。
- `/internal/v1/health` 和 `/internal/v1/version`：服务探针。
- `billing-internal.openapi.yaml`：余额、消费、充值、退款内部契约。
- Java 账务垂直链路：余额查询、模拟充值和模型调用扣费，默认关闭且未接入生产流量。
- Python `RepositoryStore` 已支持通过 `JOB_AGENT_JAVA_BILLING_ENABLED` 切换到 Java 充值和扣费；充值幂等键沿用充值请求的 `idempotency_key`，模型调用使用 `call_id`。
- 开关关闭时 Python 保持原有账务读写路径；开关打开后，余额查询、演示充值、模型扣费和人工补款由 Java 负责，Python 在这些路径只调用接口并读取已提交结果。支付 Webhook/退款等尚未迁移的路径不能据此视为 Java 所有。外部 `/api/auth/me` 和 `/api/me/balance` 的 URL 与响应契约保持不变。
- CI 的 Python-Java contract check 会验证模拟充值、模型扣费、数据库流水和幂等重试。

## 第二阶段交付：凭据校验迁移

- 第二阶段使用独立凭据校验接口；第四阶段已删除该中间接口和 Python 客户端方法，改为原子登录会话接口。
- Java 同时兼容现有 Python 写入的 Argon2id 和旧版 scrypt 哈希，不要求批量重置用户密码。
- 当时 Python 保留注册、邮箱验证、Session Cookie 和登录后的账号读取；注册和邮箱验证已在后续阶段迁移，当前边界见下文。
- Python 通过 `JOB_AGENT_JAVA_AUTH_ENABLED`、`JOB_AGENT_JAVA_AUTH_BASE_URL` 和
  `JOB_AGENT_JAVA_AUTH_INTERNAL_TOKEN` 开关此迁移，默认关闭。Java 服务对应使用
  `PLATFORM_AUTH_ENABLED=true` 和相同的 `PLATFORM_INTERNAL_TOKEN`。
- Java 认证服务不可用时，Python 返回 503，不会静默回退到 Python 密码校验，避免双写/双事实源造成行为不一致。
- 本地验收脚本会在隔离 PostgreSQL schema 中验证注册账号、Java 会话、密码修改/重置和后续账务请求。
- 注册请求通过 Java 内部接口完成；账号、零余额摘要和协议同意记录在一个事务中写入，Python 保留外部 API 兼容。

## 第三阶段：验证与密码重置

邮箱验证和邮箱变更已迁移到 Java；注册账号与初始验证邮件任务在同一事务中创建，已登录用户的邮箱变更先校验 Java Session，再向目标邮箱发送一次性确认链接。
`platform_account_action_emails` 是 Java 独占写入的验证/变更/重置投递账本，Python SMTP Worker
通过内部接口领取任务、投递并回报，不直接写此表。密码重置、邮箱变更令牌消费和旧会话撤销在一个
Java 事务中完成；重置令牌绑定签发时的密码摘要，邮箱变更令牌绑定目标邮箱，防止旧链接或冲突邮箱覆盖当前账号。
登录并发保护现已由下一阶段 Java 会话事务接管。
升级保留已有 Java 验证链接；切换 Java 后旧 Python 操作链接失效，应重新请求，不静默回退。
Java 模式不再派发任何旧 Python 账号邮件，旧队列消息也被跳过；关闭迁移开关时仍保留兼容路径。

## 第四阶段：会话与密码修改

Java 接管登录、会话解析/续期、单设备退出、全部设备退出和修改密码。
账号行锁统一串行化登录、修改密码和密码重置；登录校验与会话写入不再跨服务分两步。
Session 原文不入库、不写日志；迁移前由 Python 网页适配层设置 Cookie，当前直达 Java 的认证路径由 Java 设置；保留闲置 7 天/绝对 30 天语义。
Python 保留未分流的外部 API、Cookie/CSRF 和页面适配；账号展示读取通过 Java 的安全账号投影完成，一次 HTTP 请求内共享 Java 会话解析结果。
Java 故障一律失败关闭，不退回 Python Session 数据库路径。
纯 Python 分支和关闭开关的兼容模式仍依赖 `auth.py` 与仓储方法，因此不能直接删除整个模块。
已删除混合模式原有的“Java 校验、Python 签发”的中间流程。

管理员账号列表、当前账号资料读取、显示名称写入、启用/停用、首次管理员引导和账号注销准入/完成已经迁移到 Java。Java 在真实 Session
校验后执行管理员授权，并在同一事务内完成状态变更、目标会话撤销、旧账号操作链接
失效和管理员审计；Python 只做网页 API 兼容转换。注销的数据清理仍保留在 Python
后台任务中，因为它还需要对象存储、知识库、后台任务和财务保留数据的可恢复删除编排，
不能用一次同步 SQL 搬运替代。Java 负责在真实 Session 和密码校验后立即停用账号、
撤销 Java Session 和失效账号操作链接；Python 只收集对象键、取消自己的排队任务并执行
跨系统清理，成功后调用 Java 完成账号匿名化。Java 的完成接口可安全重试，Python 任务
重试不会重复匿名化账号。
当前账号资料通过 `/internal/v1/auth/accounts/me` 返回安全投影，Python 只把它转换成原有 `AccountRecord`
供外部 API 使用，不再从本地 `accounts` 表补全 Java 模式下的当前账号。开关关闭时仍使用原有本地读取路径。
本阶段不改变数据库结构，不需要新增迁移；跨数据库版本生产回滚仍需单独验收。

## 目标入口与发布规则

最终由 Java 接管对外业务 API，Python 提供内部 AI/长任务能力，保持现有前端 URL/API 兼容。
混合部署入口现在按路径分流：认证、已迁移的账号/账务/管理/档案和聊天路径由 Caddy 或
共存 Nginx 直达 Java，页面静态资源及其余 API 仍进入 Python Web。Java 独立执行 Session、Redis
限流、CSRF、安全响应头和请求观测；Python 通过内部 Token 调用 Java 认证事实源处理尚未
迁移的账号 API。两端共享 Java Session 和同一个 PostgreSQL，但只有 Java 写认证事实。
纯 Python 分支的入口和代码不受这个混合路由影响。网关拒绝公网 `/internal/*`。

Java 与 Python 统一使用 W3C `traceparent` 传播上下文，同时保留现有
`X-Request-ID`/`X-Trace-Id` 兼容关联字段。Java 的 Spring/Micrometer Trace 默认关闭，
生产通过 `PLATFORM_TRACING_ENABLED=true` 导出到与 Python 相同的 OTLP Collector；日志只
写 Trace/Span ID、请求 ID、路由和状态，不写 Cookie、账号标识或请求正文。Trace 后端不可用
不阻断业务请求。

本地使用 `compose.yaml + compose.platform.yaml`，可通过 `hybrid-local` profile 在
`127.0.0.1:18082` 验收同一分流；生产加载 `compose.yaml`、`compose.prod.yaml` 和
`compose.hybrid.prod.yaml`；共存部署另加 `compose.coexist.yaml` 与
`compose.hybrid.coexist.yaml`。运行环境需将 `PLATFORM_TRUSTED_PROXY_CIDRS` 限定为
实际受控网关的地址段，避免所有访客共享一个 Java IP 限流桶。
日常推送只运行快速 CI；提交说明包含 `[full ci]` 或手动触发完整 CI，通过后才发布相同提交的 `-ai` 和 `-platform` 两个版本镜像，审批部署通过 `Deploy Python Java`
工作流执行；生产只填环境配置，不需要修改代码/Compose。此分支不自动部署服务器。

后续继续迁移管理/账务外部入口。所有 GitHub 推送必须确认对应提交的快速 CI 通过；
发布里程碑则必须确认完整 CI 和双镜像发布均成功。

## 管理员人工补款迁移

混合模式下，`POST /api/admin/accounts/{account_id}/balance/credit` 保留 Python 网页适配入口，
但资金写入统一调用 Java `POST /internal/v1/billing/credit`。需要同时启用 Java 账务与认证；
Java 验证内部服务 Token、真实管理员 Session、目标账号、金额、补款原因和幂等键。
过期、撤销或停用的会话即使重放已成功的补款也不能通过；Java 不信任仅由 Python 提供的操作者 ID。

补款只创建 `adjustment` 流水，不创建支付订单、不增加充值收入。余额、流水与
`balance.manual_credit` 审计在同一个 Java 事务提交；审计失败会整体回滚。
幂等键沿用 `admin-credit:<管理员ID>:<请求幂等键>`，重复请求返回原始流水，
同一键修改目标账号、金额或原因返回 409。会话明文不写入流水、审计或错误日志。

管理员操作复用账号管理的事务级策略锁，账务写入先锁账号再锁余额，账号锁允许外键检查，
避免两个管理员互相补款或管理员自补款与模型扣费之间形成锁循环。
Python 收到成功响应后只读取已提交的流水；Java 错误或网络超时不会触发 Python 余额写入。
关闭 Java 账务开关时仍保留本地兼容路径；真实支付渠道仍未接入，
但模拟退款和充值对账已经由 Java 账务边界负责，本阶段不代表真实支付渠道已经可用。

## 个人账务查询直达 Java

混合网关把 `GET /api/me/balance`、`GET /api/me/recharge/orders` 路由至 Java，
原有页面、URL、JSON 字段及 100 条分页上限不变。查询通过 GET 匹配，充值通过独立 POST 匹配。
Java 从 Web 安全过滤器校验后的 Session 获取账号，内部适配接口也要求服务 Token 和真实 Session，
不接受调用方指定查询归属。余额摘要、统计和列表通过只读 Repeatable Read 事务提供同一快照，
时间相同的记录按 ID 倒序，保留空字段及 UTC 来源时间。`/api/auth/me` 复用同一余额查询服务。
Java 模式的 Python 个人查询不再直接读取本地流水或订单；故障失败关闭，不返回旧余额。
价格、低余额阈值及演示充值开关由 Compose 将同一环境参数注入两端；生产默认关闭演示充值。
资金事实不自动淘汰。真实支付渠道仍是后续范围；模拟充值退款和对账由 Java 提供。
验收在隔离数据下暂停 Python Web，验证网关余额及订单查询仍由 Java 正常响应；不会暂停开发或生产服务。

## 个人演示充值入口直达 Java

`POST /api/me/balance/recharge` 经 Caddy/Nginx 直达 Java，前端及 `order/entry/summary` 响应不变。
Java Web 安全层负责 Session、CSRF、Redis 写请求限流；资金事务内再次校验 Session 并锁定账号，
避免停用或密码撤销与充值竞争。开关、单笔和累计额度由服务端配置决定，不信任调用方指定的账号或额度。
充值沿用原有账务事务，订单/余额/流水/支付事件一起提交；返回结果也在同一事务内组装。
相同账号/幂等键/金额返回原订单和流水及当前余额；金额变化返回 409，缺省键代表新操作。
Python 在 Java 模式下只调用新的内部适配接口，不再读取本地充值结果或执行额度规则，故障不回退。
关闭 Java 开关的纯 Python 兼容路径仍保留；本次公开入口继续不接入真实支付渠道，
但管理员可以对已到账模拟充值执行全额退款，并读取订单/流水/退款冲销的一致性报告。

当前已补齐真实支付的渠道无关基础：Java 可登记不改余额的待支付订单，渠道回调统一进入
`POST /api/payments/webhooks/{provider}`。回调必须携带时间戳和 HMAC-SHA256 签名，默认只接受
5 分钟内的请求；事件按渠道和事件 ID 幂等，成功事件在同一事务中更新订单、余额、流水和支付事件。
具体微信/支付宝适配、渠道退款状态机和渠道对账仍待实现，生产默认关闭 Webhook；
当前已实现的是不调用外部渠道的模拟订单全额退款和内部账务对账。
隔离 Docker 验收会暂停 Python Web 后实际充值，验证 Java 独立到账、重复请求及额度限制。

## 管理员账务公开入口直达 Java

混合网关仅将以下公开路径分流到 Java，不使用笼统的 `/api/admin/*` 转发：

- `GET /api/admin/balance/events`
- `GET /api/admin/recharge/orders`
- `GET /api/admin/recharge/orders/{order_id}/events`
- `GET /api/admin/recharge/reconciliation`
- `POST /api/admin/recharge/orders/{order_id}/refund`
- `POST /api/admin/accounts/{account_id}/balance/credit`

前端及原有 JSON 不变。Java 直接用 Session 校验管理员权限，补款以“策略锁 → 操作者 →
目标账号 → 余额”顺序在同一事务中校验权限、写入账务与审计并组装响应，避免交叉补款死锁。
查询在独立只读 Repeatable Read 快照中统计总数与读取分页；资金记录不按五页规则删除。
相同操作者、幂等键、目标、金额和去空白原因重试，只返回原流水及当前余额，不重复补款或审计。
Python Java 模式只转发会话及表单，不构造补款审计、不读取充值/流水结果，故障失败关闭。
退款要求已到账订单、金额完全匹配且余额足够；成功后订单状态变为 `refunded`，
充值收入总额保持原始充值总额，退款金额通过负向调整流水表达。重复退款只返回原结果，
不会重复写流水、事件或审计。对账报告会检查已支付订单缺少充值流水、孤立充值流水、
退款订单缺少冲销流水和重复充值流水。后台任务和请求观测仍属 Python；本轮不代表整个管理后台已迁完。
本地 Docker 验收暂停隔离 Python Web，验证管理员查询、自补款/他人补款、越权拒绝、CSRF 和撤销后的重放拒绝。

## 管理员账号与审计公开入口

`GET /api/admin/accounts`、`PATCH /api/admin/accounts/{account_id}/status` 和
`GET /api/admin/audit/events` 按具体方法和路径分流到 Java，其他后台功能不变。
列表及状态变更复用 Java 账号管理事务，公开投影将内部 `account_id` 映射为 `id`，
不包含密码或 Session。启停沿用策略锁、自停用/最后管理员保护、会话撤销、操作链接作废和同事务审计；
启用账号不复活旧 Session。状态审计携带网关规范化的请求 ID，Python 兼容入口也透传相同字段。
审计查询使用真实管理员会话和有界分页，默认 50、最大 200 条，按时间和 ID 倒序。
保留可空操作者/目标/请求 ID 等历史字段，同时读取 Java 与 Python 写入同表的事件，不新增清理策略。
Python Java 模式将审计请求发给新的内部服务接口，失败关闭，不回读本地审计表。
Docker 验收暂停隔离 Python Web 后验证账号列表、启停、审计及权限/CSRF/会话撤销；
数据库验收同时验证审计写入失败时状态和会话撤销一起回滚。无需修改数据库结构。

## 管理员用量查询

`GET /api/admin/usage/summary` 和 `GET /api/admin/usage/events` 经混合网关直达 Java。
Java 校验真实管理员会话后，在只读 Repeatable Read 快照中读取汇总或分页总数/明细；
保留现有字段、可空引用、账号/档案/会话过滤和稳定时间/ID 排序。每个账号先取最新 500 条，
再筛选和分页，默认/最多每页 100 条、最多保留 5 页；全局查询可包含多个账号的窗口。
汇总中的工具任务数也使用每账号最新 500 个任务，不搬迁工具详情或执行逻辑。
余额投影只统计未删除账号，保留账号状态与各账号阈值；资金事实不受 500 条窗口限制。

Python 继续独占写入 Token/工具记录和执行写入裁剪、后台定期物理清理；Java 查询不会删除任何记录。
历史超量记录在后台清理前也不会被 Java 读出来。无需数据库结构迁移。
混合模式的 Python 兼容入口仅转发会话、过滤参数和请求 ID，故障不回读数据库或旧缓存。
Java 汇总暂不使用缓存，两端写入后下一次查询直接读取已提交事实，避免跨语言缓存失效遗漏；
纯 Python 模式仍使用原有缓存。未来若缓存该投影，应先建立跨服务可靠失效机制。
Docker 验收会由 Python 写入非计费用量，再暂停隔离 Python Web，验证 Java 汇总/明细、
筛选分页、补款后的余额与停用状态立即更新、普通账号越权和旧会话重放拒绝。

## 管理员工具轨迹查询

`GET /api/admin/tools/traces` 和 `GET /api/admin/tools/traces/{root_request_id}` 经网关直达 Java。
查询复用真实管理员 Session/RBAC、请求 ID、Redis 限流及 no-store 响应头，内部适配需要服务 Token 和真实会话。
列表在只读 Repeatable Read 快照中统计总数和读取摘要，默认/最多 100 条；每账号取最新 500 条，
按更新时间/ID 倒序，详情也只能访问窗口内记录。未知或已淘汰任务返回 404，无效分页或超长任务 ID 返回 422。
列表不查询或返回 `trace_json`，详情才按需读取已由 Python 保存的低敏快照。
Java 不重排步骤/重试，不重新解释工具结果，保留成功/失败、失败摘要和待确认状态，不新增原始参数、正文或隐藏推理。
历史非对象或空 JSON 仍按空对象返回，与 Python 兼容；不缓存详情，后台进度更新后下一次查询读取最新提交。
Python 继续负责真实工具执行、轨迹生成/持久化和物理裁剪，Java 不写或清理工具表，无需数据库迁移。
混合模式的 Python 页面 API 只转发，不回读本地记录或执行查询前裁剪；纯 Python 路径保持不变。
隔离 Docker 验收由 Python 生成包含成功结果/失败原因/待确认状态的任务，再暂停 Python Web，
验证 Java 列表、详情、分页、越权拒绝和会话撤销；不停止日常开发容器或部署服务器。

## 后台任务只读入口

仅将 `GET /api/admin/tasks` 和 `GET /api/tasks/{task_key}` 分流到 Java。
管理员列表校验真实会话和角色，支持账号/类型/状态过滤、默认及最多 100 条分页，稳定按更新时间/ID 倒序。
总数与结果来自同一只读 Repeatable Read 快照；个人进度只查询当前账号，即使管理员也不能越过归属。
未知或其他账号任务统一 404，未知状态 400，无效分页/超长标识 422。
显式列投影排除输入 payload、幂等键、Session 和内部 ID，个人返回 12 个进度/结果字段，管理员额外包含账号/档案 ID。
不缓存，Python Worker 已提交的状态、尝试次数、结果和错误可在下次查询中读到。
分页中的 `max_pages=5` 仅保留现有 UI 协议，不设置 500 条任务窗口，更不删除运行中或等待恢复的任务。
Python 继续独占投递、重试、认领、状态写入及恢复；网关按方法和精确路径分流，写请求仍由 Python 处理。
Python Java 模式的兼容入口只转发会话、筛选和请求 ID，异常失败关闭；纯 Python 模式保持原行为。
无需数据库迁移；隔离 Docker 验收覆盖 Python 写进度、Java 立即读取、账号隔离、分页和 Python Web 暂停后的查询。

## 管理员任务操作入口

`POST /api/admin/tasks/probe` 和 `POST /api/admin/tasks/{task_key}/retry` 经网关进入 Java。
Java 校验真实管理员 Session、CSRF 和限流，以操作者与 `Idempotency-Key` 生成不含会话的命令 ID，
调用受服务 Token 保护的 Python `/internal/v1/task-commands`。Python 再验证真实会话和角色，不接受调用者自报账号。
Java 不开启数据库事务等待 HTTP，也不写任务表；Python 独占命令、任务、Worker 状态和投递审计。
混合模式的 Python 兼容入口只转发至 Java，回调使用独立线程池避免兼容入口耗尽同步线程造成互相等待。

迁移 `20261017_0024` 新增 `task_commands`，同一事务登记任务或重置失败任务、保存命令和请求审计。
原始会话和幂等键不落库；命令重复不新增任务/审计，也不会重置已运行或完成任务；相同键变更意图返回 409。
任务登记提交后，dispatcher 锁定命令行并进行一次有界 Redis 投递，确认后才返回成功；
网络/队列异常返回 503，命令仍保留 pending，原子认领确保并发 dispatcher 不同时发布同一命令。
每分钟维护任务同时补投 pending 命令，每批最多 20 条；重试失败的命令更新时间使后续请求有机会处理。
停用/删除的账号不能继续补投普通业务任务，但注销清理任务允许由有效管理员重试，避免先停用后清理的状态机失效。
旧代命令不会覆盖新代重试；未投递即被取消/失败的命令返回冲突，不误报投递成功。命令随任务物理删除，不裁剪资金事实。
这属于至少一次消息投递，不承诺跨 PostgreSQL/Redis 的 exactly-once；丢失确认时仍可能重复发送消息，
Worker 继续使用原子认领和既有业务幂等机制防止重复副作用，不把 Celery task_id 当成自动去重保证。

浏览器重试按钮在请求中禁用，503/网络超时后保留幂等键，成功后清理；列表刷新失败不误报任务操作失败。
没有提供幂等键的调用按新操作处理；API 调用方需要自己保存并复用该键。
本地/生产网关同步具体 POST 路径，内部接口不对公网开放。隔离 Docker 验收使用独立 Redis broker 和真实 Worker，
验证鉴权、CSRF、命令重放、改意图冲突、Python 兼容回调、队列故障和恢复，不接触服务器或日常容器。

## 个人中心资料与邮箱申请入口

`PATCH /api/account/profile` 和 `PATCH /api/account/email` 经混合网关直达 Java，保留前端地址与响应字段。
Java 复用既有显示名事务和邮箱变更 outbox 服务，校验当前 Session、CSRF、参数与限流；身份只来自真实会话，来源使用可信代理解析结果。
Python 的兼容 API 仅转发原始字段与真实 Session；混合模式不再读取本地账号、不重复判断邮箱归属或写旧投递表，Java 故障失败关闭。
纯 Python 模式维持原逻辑。本次不修改数据库结构、不新增配置项，三个本地/生产网关文件同步分流。
邮箱确认和旧会话撤销沿用 Java 事务；SMTP/Celery 投递仍属 Python，两个 HTTP 入口独立于 Python Web 不等于邮件脱离 Python Worker。
本地隔离 Docker 验收覆盖兼容转发、Python Web 暂停时的修改与申请、邮箱冲突、CSRF、空显示名、冷却去重、邮件处理回报与确认后旧会话撤销。

## 请求观测统一入口

`GET /api/admin/observability/requests` 经本地和生产混合网关直达 Java，复用真实管理员 Session/RBAC、限流、请求 ID 和 no-store。
Python 兼容入口仅转发真实 Session，不在 Java 故障时回读本地快照或邮件表。
Java 向 Python 私有 `GET /internal/v1/observability/requests` 获取低敏快照，使用共享服务 Token，不携带 Cookie。
回调采用独立于 FastAPI 同步处理池的线程执行，避免并发兼容入口等待 Java 时耗尽回调线程。
连接最多 1 秒、完整响应最多 2 秒、响应体最多 1 MiB，不重定向；生产目标必须是内部服务地址。

两端各维护 `/api/*` 和 `/internal/*` 两套线程安全快照。仅对外 API 相加，内部调用不加入总数，避免跨语言调用重复计数。
耗时按总耗时/总请求数加权，最大耗时取最大值；聚合 endpoint 加 java/python 前缀，最近错误最多 20 条并带服务来源。
端点只用路由模板或 unmatched/other，方法和结果有界，不保存任意 URL、查询参数、正文或 Cookie。
Java 邮件三种用途和 Python 历史投递记录分别读取，合并最新 20 条，保留 owner，收件地址及错误显式脱敏。
任一邮件来源不可用时邮件汇总未知，仍可显示有效请求指标；任一请求来源不可用时 API 合计为 null，仍保留另一端数据。

后台允许切换 API 合计/单端 API/单端内部调用；真实 0 与不可用分开，刷新失败保留旧快照并提示过期。
观测刷新不阻塞账号与用量展示，过期的并发响应不能覆盖较新数据。纯 Python 模式仍可使用，Java 标为未配置。
快照随各进程独立重启清零，不缓存、不中转到 Redis、不写数据库，也不声称是跨副本全局累计。
原有 Prometheus 采集与告警不改口径；需要历史趋势或多副本汇总时使用 Prometheus。
无数据库结构迁移。隔离 Docker 验收覆盖网关分流、加权合计、兼容回调、Python 暂停降级及恢复和权限拒绝。

## 账号导出入口

混合网关将 `GET /api/account/export` 分流到 Java。Java 校验真实 Session 后结束账号事务，
调用 Python 私有 `POST /internal/v1/accounts/export`；回调必须同时具有服务 Token 和真实 Session，
Python 再向 Java 解析身份，不接受账号 ID。账号投影来自 Java，Python 不重复读取账号密码或 Session 表。
Python 保留业务表导出与来源关联的所有权，Java 不直接读取 AI 业务表；Python 兼容公开入口仅转发到 Java，故障不回退。

Java 检查导出根字段、账号、各直接记录与余额的 account_id，以及版本/文件记录的父级归属，拒绝额外的凭据字段；
用户撰写的文本和 JSON 内容不做关键词删改。公开附件名由已校验账号 ID 生成，不转发上游 Cookie 或附件头；所有下载 no-store。
数据库 Decimal 以十进制字符串导出，避免金融精度在 JSON 浮点转换中丢失。
Java 连接上限 1 秒、完整回调上限 30 秒、不跟随重定向、不使用系统代理；两端每进程最多同时执行 2 次导出。
混合路径传输上限 16 MiB，超出返回 413，不静默截断；上游异常/超时返回 503，撤销的 Session 返回 401。
回调使用独立线程执行，避免 Python 兼容同步线程等待 Java 导致死锁。

此步没有数据库迁移，Compose 自动连接内部服务；本地与生产网关同步支持，无需改业务代码才能部署。
导出仍是同步、内存内聚合，不是全库一致性备份或大规模流式归档；纯 Python 旧路径保持兼容。
超大账号后续应改为异步分批导出。

## 账号注销准入与可靠恢复

混合模式 `POST /api/account/delete` 经本地/生产网关进入 Java，校验真实 Session、CSRF、密码和“注销账号”确认文本。
同一 PostgreSQL 事务完成停用、撤销会话/凭据链接、写入审计和 `platform_account_deletion_requests`；任一步失败全部回滚。
成功返回 202 和固定 task_key，并清除浏览器 Cookie。202 仅表示受理，不表示文件已经清除；Python Web/Redis 故障不影响登记。
Python 兼容入口只转发真实 Session，不再在公开处理函数中停用账号、创建任务或投递；纯 Python 模式保持原行为。

既有 Beat 每 60 秒触发维护，Python Worker 使用服务 Token 向 Java `/internal/v1/accounts/deletions/due` 领取最多 20 条到期意图。
Java 用行锁/SKIP LOCKED 和 60 秒领取窗口分开并发批次；未完成意图持续保留、到期重领，避免丢确认或进程退出永久失联。
该恢复接口不使用已撤销的用户 Session，不对外开放；只返回任务定位，不保存密码或 Session。
Python 事务按固定 task_key 幂等创建清理任务、取消其他排队任务，提交后再投递 Redis。
队列故障保留 queued，恢复后仍投递同一任务；至少一次投递依赖 Worker 原子认领，非跨库/Redis exactly-once。
已失败任务不自动重置预算，管理员沿用任务列表的人工重试；运行中的任务不重复执行。

普通任务认领与账号停用通过账号行锁串行化，停用后的普通任务不执行。
清理 Worker 等待已有 running 任务结束，重新收集并持久化合并后的对象清单，随后删除对象、业务表与 PostgreSQL RAG 索引。
部分删除/跨服务确认故障保留清单，重试安全重复删除；Java 最后验证注销任务 key、匿名化账号、移除账号凭据及邮件并登记完成。
保留必要的匿名化资金事实，不把未清理完的 disabled 账号重新启用。
长时间运行任务或持续存储故障可能耗尽清理重试预算，届时需管理员排障并重试；Beat/Worker 停止期间意图不会丢失，但不会自行执行。

迁移 `20261018_0025` 只补录具有明确旧注销准入审计的 disabled 用户；普通管理员停用、或准入后有管理员状态干预的账号不会自动清理。
复用已有清理 task_key，避免重复任务。无新手填配置，沿用现有 Java 服务 Token、Worker/Beat 和生产部署迁移流程。
旧内部 `delete-admission` 协议在鉴别服务 Token 后返回 409，不再停用账号，避免旧 Python 再创建不匹配的随机任务。
首次混合版切换应在维护窗口停收注销写入并发布同 SHA 镜像对，不支持把不同协议代际的 Web/Worker/Java 长期混跑。
本分支只进行本地隔离 Docker 验收，不触碰 master 或真实服务器。

## 注销前的在途请求排空

Python 通过 `current_account` 接受的请求持有 PostgreSQL 账号级共享 advisory lock，
拿锁后重新检查账号 active/deleted_at，避免使用 Java 请求内缓存的旧认证结果绕过停用。
锁由 ASGI 路由在完整响应结束后释放，覆盖同步写入、上传处理、SSE 及响应后台任务；
SSE 断开时显式关闭 Web/Agent/LangGraph 迭代器，再释放账号锁，不依赖垃圾回收触发收尾。
内部账号导出在线程内独立持锁，HTTP 调用方断开不会提前释放仍在工作的导出线程。
原生同步注销入口不持有自身请求锁，以免清理等待自己。

注销 Worker 先尝试同账号排他锁，再检查 running 后台任务、更新对象清单和执行清理。
已有请求可以完成，但清理不会与之并行；停用后的新请求不能进入。
混合模式拿不到锁或后台任务尚未结束时，把注销任务放回 queued 并归还本次尝试次数；
Java 的持久化意图由既有 60 秒维护周期再次投递，不忙轮询，也不把正常等待算作清理失败。
实际存储/数据库/Java 确认故障仍使用原有有限失败预算和管理员重试。

保护连接使用独立 NullPool 和 autocommit，不长期持有业务事务或占满业务连接池；
每个 Python Store 实例最多额外持有 8 个保护连接，超限/数据库不可用拒绝新请求并返回脱敏 503。
当前 Web 每进程一个 Store，因此容量规划须计入每进程最多 8 个额外连接。
schema 参与锁键，隔离测试不会阻塞正式数据；物理会话关闭/进程退出由 PostgreSQL 释放锁。
使用当前直连 PostgreSQL 拓扑，不支持把保护连接改成 PgBouncer transaction pooling。
此保护不是跨数据库故障切换的 fencing 协议：数据库重启/切换应停收并排空 Web/Worker，
不能假设失去数据库会话的业务线程也已终止。不同协议代际的旧 Web 不持锁，首次升级仍需维护窗口。
无需新环境变量或数据迁移，本地及生产镜像使用同一实现。

## 工作台档案只读入口

`GET /api/profiles` 和 `GET /api/profiles/{candidate_id}` 经本地/生产网关进入 Java。
Java 校验真实 Session 后结束认证事务，再使用独立服务 Token、原始 Session 和请求 ID 回调 Python
`POST /internal/v1/profiles/query`；不使用调用方提供的账号 ID，不直接查询 Python 档案表。
Python 再向 Java 验证会话，持有账号排空共享锁并按 account_id 调用原有应用门面和 Cache-Aside 缓存。
回调放在独立线程执行，最多 8 个并发，避免 Python 兼容同步线程等待 Java 导致回调死锁；HTTP 取消不会提前释放仍在工作的查询线程的锁。

Python 回调返回 account_id 加 profile/profiles；Java 严格验证归属、详情 ID、字段白名单/类型及列表 ID 唯一性，去掉内部归属字段后保留现有前端契约。
列表仍按原有 ID 顺序全量返回，不静默分页/截断；完整响应限 4 MiB，超限显式 413。
Java 回调连接最多 1 秒，完整响应最多 2 秒，最多 8 个并发，不重定向/自动重放；生产只接受内部服务 origin。
该超时限制 HTTP 等待，不宣称能中止已经执行的 Python/SQL 查询；服务故障/协议错误返回 503 和 Retry-After，不回退数据库或伪造空列表。
只有符合已知错误码的真正 missing/foreign 详情返回 404；服务路径错误或内部凭据错误按依赖故障处理。
外部响应 no-store；Java 不建立第二份档案缓存。现有 Python 档案缓存策略及写后失效不变。

Python 公开 GET 在混合模式仅转发到 Java 的同名内部入口；纯 Python 模式保持原实现。
创建、修改、删除和对话更新仍由 Python 写入，不改变档案数据所有权；创建公开入口的后续迁移见下一节。
本地 Compose 与生产继承配置均自动指向内部 web 服务，Caddy 与共存 Nginx 将档案 GET/POST/DELETE 分流 Java；实际数据写入仍由 Python 负责。
只做本地隔离 Docker 验收和迁移分支 CI，不部署真实服务器。

## 工作台档案创建入口

`POST /api/profiles` 经 Java 的 Session、限流、CSRF 安全入口，结束认证事务后回调 Python
`POST /internal/v1/profiles/create`。Python 兼容入口只转发真实 Session、原始幂等键、档案输入和请求 ID，
不接受账号 ID，也不在 Java 不可用时本地写入。Python 再验证真实 Session，独立线程持有账号排空锁到业务完成。
规范化、内容去重、档案/初始 long_text 写入和 Cache-Aside 写后失效复用 Python 应用门面，无 Java 档案 SQL 写入或第二份缓存。

迁移 `20261019_0026` 添加 Python 所有的 `profile_create_commands`：仅存账号、key/payload 哈希、档案 ID 与时间，
不存 Session、原始键或档案文本。账号内同键使用事务 advisory lock 串行化，账号行共享锁复核 active/deleted_at。
档案、long_text 与凭据在一个事务内提交，失败全部回滚。相同键/输入返回原档案的当前投影；不同输入或档案已删除返回 409。
删除档案时 FK 置空留下墓碑，避免超时旧请求复活数据；注销清理移除该账号凭据。凭据不按短期 TTL 清除，避免过期重放重新创建。

前端同次表单失败重试保留幂等键，编辑内容或账号变化生成新键；成功清空，防止重复点击。
该状态仅在当前页面内存保留，刷新页面不会恢复原请求；不把档案文本额外写入浏览器存储。
兼容不带键的旧 API 调用者时服务端生成键，但旧调用者不能恢复已提交凭据，只能由内容去重拒绝重复。
公开响应继续为 200 `{candidate_id, profile}`，回调额外的 account_id 在 Java 校验归属与 ID 一致性后剥离。
输入序列化限 64 KiB；回调复用既有 4 MiB、8 并发、1 秒连接/2 秒完整响应限制，不重定向、不自动重试。
超时不代表数据库未提交，503 明确提示使用相同请求重试；不存在“失败后换键自动再创建”的逻辑。
生产与本地 Caddy、共存 Nginx 同步分流创建 POST；继续使用现有配置与部署迁移命令，无新增手填项。

## 工作台档案删除入口与附件恢复

`DELETE /api/profiles/{candidate_id}` 经 Java Session/CSRF/限流后回调 Python `/internal/v1/profiles/delete`。
Python 兼容入口只转发真实 Session 和档案 ID，不信任账号输入或在 Java 故障时自行删除。
Python 独立线程持有账号级排他请求锁，避免已接受的 Web/SSE/上传仍在写入；拿不到锁立即返回 409。
事务锁账号行并复核 active/deleted_at，与 Worker 认领使用的共享账号行锁串行化；存在 running 后台任务也返回 409，
不强杀任务、不部分删除。锁的数据库故障切换限制与前文一致，不能当作跨库 fencing 协议。

迁移 `20261019_0027` 添加 Python 所有的 `profile_deletions`。一个事务保存所有资产版本、简历、采集文件、视觉对象的
去重清单，并删除档案及从属数据；文本/视觉向量在 PostgreSQL 同事务级联删除。缺失/跨账号档案统一 404，
拥有该档案删除凭据的账号可重复 DELETE 查询当前状态，资源 ID 本身就是幂等标识，不需要另填请求键。
返回仅含 deleted/candidate_id/cleanup_status/rag_cleanup，Java 验证回调归属及字段后剥离 account_id，禁止泄漏对象键和原始资料。

HTTP 回调不等待对象存储。无附件时 200/completed；存在附件则 202/pending，代表事实已删除、附件仍待处理，前端明确提示。
现有 Beat 60 秒维护周期扫描到期清单，每次最多 10 条，每条最多 20 个对象，短事务认领 5 分钟租约；
网络删除在事务外执行，成功进度再按租约令牌提交。Worker 重启/失联后租约过期重领，已删对象可安全重复删除。
存储失败保存剩余清单和固定错误摘要，退避从 60 秒增长到最多 1 小时，不丢清单、不将失败伪报完成。
没有 Worker/Beat 时清单仍在数据库，但不会自动推进；停机或持续存储故障时也不会保证 60 秒内完成。
同次调用删除完成但提交进度失败时，会重新删除相同不可变对象键，属于可恢复的至少一次清理，不是跨系统 exactly-once。

删除和重放均失效原有档案缓存；缓存命中还做轻量删除凭据校验，阻止失效失败或迟到填充把档案重新展示。
账号注销的原生/混合清理都将未完成档案清单纳入对象清单，清完后才移除档案删除凭据，避免注销遗失附件。
内部应用同步便利方法仍可尝试最多 100 个对象，失败保留 pending；公开接口始终异步清附件，200/202 不混淆。
无需新手填配置，本地/生产网关与现有部署迁移命令同步；本分支只进行本地隔离 Docker 验收，不部署真实服务器。

## 聊天会话管理入口

`GET/POST /api/chat/sessions`、`POST /api/chat/sessions/{session_id}/archive`、
`DELETE /api/chat/sessions/{session_id}` 和 `GET /api/chat/history` 经混合网关进入 Java。
Java 使用真实 Session、CSRF、Redis 限流和请求 ID，认证事务结束后向 Python
`POST /internal/v1/chat/sessions/{list|create|archive|delete|history}` 回调；Python 公开兼容入口仅转发到 Java。
两端内部接口均要求服务令牌，网关拒绝所有 `/internal/*`。契约见 `chat-sessions.openapi.yaml`。

Python 再验证同一 Session，从真实账号确定档案/职位/会话范围，复用原有会话和消息业务实现。
回调独立线程持有账号工作锁；删除使用排他锁，已有 HTTP/SSE 请求未结束时返回 409/CHAT_BUSY，
避免迟到回复复活会话。Java 验证回调 account_id、档案及会话定位、字段白名单、重复记录和消息上限，
剥离内部 account_id 包装，但保留原会话行本来就有的 account_id。历史继续使用已有文本和元数据脱敏。

查询默认隐藏归档会话，include_archived 可显式列出；历史 limit 默认 100、最大 500，
列表不静默分页或截断，完整响应超过 4 MiB 返回 413。回调连接超时 1 秒、完整响应期限 10 秒，
并发最多 8 个，不自动跟随重定向或重放写入。创建超时可能已经提交，提示刷新会话列表核实，
本轮没有增加创建幂等凭据；重复归档返回归档记录，重复删除返回 404。归档历史仍按原规则返回 409。
会话删除保留 Token/账务流水，不影响档案事实。失败返回显式错误，禁止伪装空列表或回退本地数据。

本地、生产 Caddy 和共存 Nginx 同步分流，会话回调默认连接 web:8000，
Compose 自动设置连接地址，无需新增手填配置或数据库迁移。SSE、RAG、
对话摘要与 Worker 继续由 Python 执行，普通聊天入口见下节。本分支只在本地隔离 Docker 验证。

## 普通聊天入口

`POST /api/chat` 在本地/生产混合 Caddy 和共存 Nginx 中按精确路径进入 Java；
`/api/chat/stream` 的迁移见下节。Java 校验真实 Session、CSRF、请求字段，
使用 Redis 的独立 model 限流组，按 IP 和账号分别计数。
`JOB_AGENT_RATE_LIMIT_MODEL_REQUESTS` 沿用默认每 60 秒 60 次；窗口仍由原窗口配置控制。
Java 鉴权结束后调用 Python `/internal/v1/chat/invoke`，不持有数据库事务等待模型。
Python 公开 `/api/chat` 在混合模式仅调用 Java 同名内部兼容入口，禁止本地执行回退。

两端内部入口必须携带服务令牌和真实 Session。Python 再验证 Session、档案和会话归属，
复用唯一 `execute_chat`，保留 Agent、工具、RAG、低敏任务轨迹、历史落库和原有 Java 扣费链路。
输入只接受 candidate_id、message、session_id、use_env_llm、auto_rag，禁止指定 account_id；
message 最多 16000 个 Unicode 字符，ID 必须为正整数，布尔标志不接受字符串。
响应仍为 mode、reply、display_reply、task_trace、background_tasks、profile 六字段。
内部包装另含 account_id、session_id；Java 校验真实账号、会话、档案 ID 和嵌套字段白名单后剥离包装。
HTTP 请求 ID 贯穿 Java/Python 传输日志；Java 为每次聊天独立生成随机 execution_id，
作为 Agent root_request_id 关联工具和账务。Python 日志记录两者映射，但不记录正文或会话令牌。
禁止把可重复的客户端请求 ID 作为账务幂等键；相同追踪 ID 的两次独立聊天必须分别结算。

回调共用最多 8 个本机槽位，连接期限 1 秒；普通聊天完整响应期限最多 300 秒（会话管理仍为 10 秒），
响应上限 4 MiB。Python 兼容转发读取超时 310 秒，网关响应等待 330 秒。应用传输不重试、不跟随重定向，
共存 Nginx 显式关闭下一上游重试；模型 SDK 自身的单次调用重试策略保持原配置，不等于重放整个聊天命令。
客户端断开或 Java 超时不代表 Python 工作已经取消；执行线程继续持有账号工作锁，
直到工具、消息和结算处理结束，防止注销/删除与迟到写入交错。普通聊天尚不提供幂等请求或强制取消。
超时、断链、非法回包等不确定结果返回 503/CHAT_EXECUTION_UNCONFIRMED，
提示先查看历史记录，不能自动重试或把结果显示成成功；没有 Retry-After 自动重发暗示。
模型异常返回脱敏 502；工具可能已产生副作用，需先检查历史与档案。余额不足明确返回
402/INSUFFICIENT_BALANCE 和“余额不足，请先充值后重试。”，不执行模型、不追加成功消息。

契约见 `chat-invocation.openapi.yaml`。隔离 Docker 验收使用私网模拟模型供应商，但运行真实
LangChain Agent、工具、数据库、网关、Java 鉴权和结算；验证一次工具更新、两条历史、
供应商 160 Token 只生成一笔消费以及零余额拒绝、依赖故障恢复。模拟供应商不打包进产品镜像，
不使用真实模型密钥或现有账号数据。无数据库迁移、无需新增手填项；本轮不部署服务器。

验收同时修复原有整轮 Token 取最大值的问题：同一个模型执行步骤内的累计 usage 仍取最大，
不同 LangGraph 模型步骤的用量相加，普通和 SSE 路径共享该规则。
例如两次各 80 Token 的模型调用应按 160 Token 结算，而不是只记录 80。

## 流式聊天入口

`POST /api/chat/stream` 与普通聊天共用 Java 输入校验、Session、CSRF 和 model 限流组。
Java 为每次请求生成独立 execution_id，不使用客户端追踪 ID 作为账务幂等键。
Python 公开兼容入口只异步转发至 Java 的 `/internal/v1/chat/stream`，不在 Java 故障时本地执行。
Java 再调用 Python 同一路径：两端均验证服务令牌，网关拒绝公网 `/internal/*`。

Python 在受限执行线程中重新验证 Session、档案、会话和模型可用性，并取得账号工作锁。
余额不足在发送流响应头之前返回 402 和统一中文提示。首个私有 scope 事件绑定真实账号、
档案、会话和 execution_id；Java 验证后剥离它，不把账号包装或 Session 发送给浏览器。
公开事件仍为 task_started、status、token、step_started、step_completed、
task_completed/task_failed/approval_required、final/error。Java 校验字段白名单、轨迹根 ID 和 final 档案 ID，
用 Spring SSE codec 解析跨网络块、CRLF、多行 data、UTF-8，而非自行拼接不完整 JSON。
JDK 响应体直接连接有背压的订阅，避免当前 Spring JDK 连接器的 cache(0) 响应体无法及时取消的问题。
参考 [Spring SSE reader](https://docs.spring.io/spring-framework/docs/current/javadoc-api/org/springframework/http/codec/ServerSentEventHttpMessageReader.html)。

Python 工作线程最多 8 个（与聊天回调共用准入槽），每条流只有 1 条排队事件；
Java 也最多同时接纳 8 条 SSE，并使用单事件消费缓冲。单事件最多 4 MiB，整条流最多 8 MiB，
最长 300 秒；Python 每 2 秒空闲时发注释心跳，Java 连续 15 秒无上游事件则中断。
浏览器现有 180 秒总等待上限仍然有效。超限、非法事件、上游提前结束且没有 final/error 时，
输出脱敏 error，绝不补造 final。所有流均 no-store，内部请求禁止重定向和自动重放。

客户端断开后，Java 关闭响应体订阅，Python 立即停止网络投递并设置协作取消信号。
不从另一个线程强行关闭正在 next() 的生成器；实际执行线程负责依次关闭 Web/Agent/LangGraph、
等待 LangGraph 已运行的同步任务结束、处理已确认用量，再释放账号工作锁和槽位。
已开始的同步模型或工具调用可能需要等到自身超时/返回；取消不是强杀，也不等于回滚工具副作用或退款。
尚未完成的对话不追加成功聊天历史；如果取消发生在成功落库之后，应以历史记录核实结果。
供应商已确认的累计用量按原调用标识只结算一次；未返回的 usage 不估算、不冒充准确账单。
存在真实工具步骤的取消回合仍保留 cancelled 审计轨迹，已经完成的步骤状态和结果不会被改成未执行。

Caddy 使用 SSE 的自动即时刷新，不设置会抑制断连取消的 flush_interval -1；
共存 Nginx 关闭 buffering/cache/next-upstream 重试，保持 proxy_ignore_client_abort off。
参考 [Caddy streaming 配置](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#streaming)。
本地和生产网关配置同步，不新增凭据、端口或数据库迁移；本分支仍不部署服务器。
冻结契约见 `chat-stream.openapi.yaml`。Docker 验收使用真实网关、Agent、工具、数据库和 Java 账务：
验证 token 先于 final 到达、160 Token 正常结算、断连不新增成功历史、保留完成的工具副作用，
只对取消前已确认的 80 Token 结算，保护锁在真实工作退出后才释放。

## Java 安全入口准备阶段（历史记录）

Java `/api/*` 已增加真实 Session/RBAC、Redis 原子滑动窗口限流、CSRF、安全响应头、
请求 ID、低基数 Micrometer 指标及脱敏错误响应。同步 Jedis 有界连接池规避 Windows Java 25
下 Lettuce NIO selector 兼容问题，也与当前 Servlet 同步访问模型一致。
Compose 本地/联合生产都启用安全过滤器，生产关闭安全开关、CSRF、Secure Cookie 或缺少 Redis
密码/强内部 Token 时拒绝启动。Redis 不可用拒绝请求，不用进程内计数替代。

原安全入口准备阶段没有切换公网流量；当前混合网关已将浏览器认证路由切到 Java，
内部服务调用仍使用 Token，与浏览器安全过滤器分开。Java 默认不信任转发头；显式配置的代理 IP/CIDR 才能提供有界、
从右向左校验的来源链，解析后的同一个 IP 用于限流和设备记录。不会信任客户端自报的协议或全网代理范围。
Java 指标已通过独立 Compose Secret 供 Prometheus 抓取，公网反向代理不开放 `/internal/metrics`。
本地 Docker 已验收网关登录、Java `/api/auth/me`、Python 余额接口和改密后跨服务会话失效；
仍不得直接开放生产 Java 端口，本分支尚未部署到服务器。
