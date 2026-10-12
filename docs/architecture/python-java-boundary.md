# Python + Java 服务边界

## 当前状态

现有 Python 服务是求职助手的唯一生产业务服务，负责 HTTP API、账号生命周期、Session、余额、Agent、RAG、文件分析和后台任务。第一阶段新增 Spring Boot 平台服务骨架；Java 认证迁移只在本地分支通过开关启用，不接管生产流量。

## 目标边界

| 模块 | 当前实现 | 目标所有者 |
| --- | --- | --- |
| Agent、RAG、OCR、视觉分析、模型网关 | Python | Python AI 服务 |
| AI 长任务 Worker | Python + Celery | Python AI 服务 |
| 账号密码校验、权限、余额、账务、退款 | Python | Java 平台服务，分阶段迁移 |
| 管理员后台业务接口 | Python | Java 平台服务，分阶段迁移 |
| PostgreSQL、Redis、MinIO | 基础设施 | 共享基础设施，按表和接口隔离 |

## 迁移规则

1. 同一业务写入操作只能有一个服务负责执行；迁移完成后收束为一张事实表一个写入所有者。
2. 迁移前必须先冻结 OpenAPI 契约、错误码、幂等键和 trace id 规则。
3. Python 和 Java 不通过直接改对方表来通信。
4. 长耗时 AI 操作使用任务 ID，平台服务不等待模型执行完成。
5. 任何新写入路径必须先有集成测试，再切换生产流量。

## 第一阶段交付

- `platform-service/`：可构建的 Spring Boot 运行骨架。
- `/internal/v1/health` 和 `/internal/v1/version`：服务探针。
- `billing-internal.openapi.yaml`：余额、消费、充值、退款内部契约。
- Java 账务垂直链路：余额查询、模拟充值和模型调用扣费，默认关闭且未接入生产流量。
- Python `RepositoryStore` 已支持通过 `JOB_AGENT_JAVA_BILLING_ENABLED` 切换到 Java 充值和扣费；充值幂等键沿用充值请求的 `idempotency_key`，模型调用使用 `call_id`。
- 开关关闭时 Python 保持原有账务读写路径；开关打开后 Java 独占余额、充值订单、余额流水和支付事件的事实权，Python 只调用接口并读取已提交结果。当前已完成余额摘要的 Java 读路径迁移，外部 `/api/auth/me` 和 `/api/me/balance` 的 URL 与响应契约保持不变。
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
混合部署的公网入口现在按路径分流：`/api/auth/*` 与 `/api/account/password` 由 Caddy 或
共存 Nginx 直达 Java，其他页面与 API 仍进入 Python Web。Java 独立执行 Session、Redis
限流、CSRF、安全响应头和请求观测；Python 通过内部 Token 调用 Java 认证事实源处理尚未
迁移的账号 API。两端共享 Java Session 和同一个 PostgreSQL，但只有 Java 写认证事实。
纯 Python 分支的入口和代码不受这个混合路由影响。网关拒绝公网 `/internal/*`。

本地使用 `compose.yaml + compose.platform.yaml`，可通过 `hybrid-local` profile 在
`127.0.0.1:18082` 验收同一分流；生产加载 `compose.yaml`、`compose.prod.yaml` 和
`compose.hybrid.prod.yaml`；共存部署另加 `compose.coexist.yaml` 与
`compose.hybrid.coexist.yaml`。运行环境需将 `PLATFORM_TRUSTED_PROXY_CIDRS` 限定为
实际受控网关的地址段，避免所有访客共享一个 Java IP 限流桶。
CI 通过后发布相同提交的 `-ai` 和 `-platform` 两个版本镜像，审批部署通过 `Deploy Python Java`
工作流执行；生产只填环境配置，不需要修改代码/Compose。此分支不自动部署服务器。

后续继续迁移管理/账务外部入口。所有 GitHub 推送必须确认对应提交
CI 最终通过，镜像发布失败也不视为完成。

## Java 安全入口准备阶段

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
