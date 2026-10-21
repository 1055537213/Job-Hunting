# Job Hunting Platform Service

这是求职助手的 Java 平台服务，承接账号认证、账务、管理端用量/工具调用记录、部分任务管理、请求观测，以及档案查询和创建的公开入口。Python 保留 AI、RAG、档案业务数据、文件和 Worker；完整职责划分见 [服务边界](../docs/architecture/python-java-boundary.md)。本分支只做本地联调，不部署现有服务器。

档案 GET/POST/DELETE 经 Java 认证入口后回调 Python，写请求同时校验 CSRF。Python 负责创建幂等事务，以及删除事实/向量和持久化附件清单；附件由 Worker 分批清理，公开返回 202 表示尚未清完，200 表示完成。Java 不建立第二份档案写入逻辑。契约见 [档案查询](src/main/resources/profile-query.openapi.yaml)、[档案创建](src/main/resources/profile-create.openapi.yaml) 和 [档案删除](src/main/resources/profile-delete.openapi.yaml)。

运行基线为 Spring Boot 4.0、Spring Framework 7、Jackson 3、Tomcat 11 和 Java 21。
依赖版本由 Maven 锁定，Tomcat/Jackson 的安全修复版本在 `pom.xml` 中显式覆盖；
CI 会扫描传递依赖，不通过忽略漏洞绕过门禁。

默认配置不会连接数据库，账务或认证模块必须显式设置 `PLATFORM_BILLING_ENABLED=true` 或 `PLATFORM_AUTH_ENABLED=true` 后才会启用 PostgreSQL。启用迁移模块后，Java 接管对应写入，Python 仍提供页面、其他 API 及 SMTP/后台任务。本分支尚未部署到现有生产服务器。

Python 侧通过 `JOB_AGENT_JAVA_BILLING_ENABLED=true` 开启调用 Java 的扣费路径；这两个开关必须同时打开，并且两边使用同一个 PostgreSQL、内部 Token 和价格配置。开发环境可使用：

```bash
docker compose -f compose.yaml -f compose.platform.yaml up -d --build platform-service web worker
```

默认仍保持关闭，避免误把未完成验收的 Java 服务接入生产账务。

## 本地运行

需要 JDK 21 或更高版本，以及 Maven 3.9+：

```bash
mvn -B test
mvn -B spring-boot:run
```

Python+Java 分支当前只做本地联调，不会部署到现有生产服务器。仓库根目录执行：

```powershell
.\scripts\validate_python_java_local.ps1
```

该命令会创建临时的 `job_agent_java_local_*` PostgreSQL schema，执行 Alembic
迁移，使用 Maven 启动 Java 服务，运行 Python 账务契约测试，最后停止 Java
进程并删除临时 schema，不会写入网页运行时使用的 `public` schema。默认 Java
端口为 `18081`；端口被占用时使用 `-Port 18082`。如需连接其他本地测试库，
设置 `JOB_AGENT_TEST_DATABASE_URL` 环境变量即可。

真实 PostgreSQL 集成测试需要本机或 CI 提供可被 Testcontainers 访问的 Docker 环境：

```bash
mvn -B -Dtest=BillingServiceIntegrationTest -Drun.integration.tests=true test
```

集成测试会验证幂等重试、并发扣费、余额不足和防止余额透支。Windows Docker Desktop
若 Testcontainers 无法连接 named pipe，普通单元测试仍可运行，应改在 Linux CI runner
中执行上述命令。

启动后检查：

```text
GET http://127.0.0.1:8081/internal/v1/health
GET http://127.0.0.1:8081/internal/v1/version
GET http://127.0.0.1:8081/actuator/health
```

启用账务模块时还需要配置：

```dotenv
PLATFORM_BILLING_ENABLED=true
PLATFORM_INTERNAL_TOKEN=local-platform-secret
SPRING_DATASOURCE_URL=jdbc:postgresql://127.0.0.1:5432/job_agent
SPRING_DATASOURCE_USERNAME=job_agent
SPRING_DATASOURCE_PASSWORD=your-password
```

启用本地凭据校验时还需要配置：

```dotenv
PLATFORM_AUTH_ENABLED=true
PLATFORM_INTERNAL_TOKEN=local-platform-secret
SPRING_DATASOURCE_URL=jdbc:postgresql://127.0.0.1:5432/job_agent
SPRING_DATASOURCE_USERNAME=job_agent
SPRING_DATASOURCE_PASSWORD=your-password
```

认证接口只供 Python 内部服务调用；完整请求/响应约束见
`src/main/resources/auth-internal.openapi.yaml`：

```text
POST /internal/v1/auth/sessions/login
X-Internal-Service-Token: local-platform-secret
X-Trace-Id: local-trace-id
```

它原子创建会话并返回 `account_id` 和仅用于设置 Cookie 的 `session_token`；错误码包括 `INVALID_CREDENTIALS`、
`ACCOUNT_DISABLED` 和 `EMAIL_UNVERIFIED`。请求体中的密码不会写入 Java 日志，
Java 兼容 Python 当前使用的 Argon2id 和 scrypt 哈希格式。Python 的登录迁移由
`JOB_AGENT_JAVA_AUTH_ENABLED=true` 控制，认证服务不可用时返回 503，不会静默回退。

注册迁移接口为 `POST /internal/v1/auth/register`。Java 在同一事务中写入账号、
零余额摘要行、协议同意记录及待验证账号的邮件任务。重复邮箱返回
`ACCOUNT_ALREADY_EXISTS`，事务失败不会留下半个账号。

邮箱验证、邮箱变更和密码重置共用 Java 独占的 `platform_account_action_emails` 账本；Python SMTP Worker
通过内部 API 认领并回报投递，不直接更新验证状态。一次性令牌只保存摘要，认领键
阻止失联 Worker 的迟到结果覆盖新任务状态。`POST /internal/v1/auth/password-reset/confirm`
在一个事务里消费令牌、更新 Argon2id 密码、撤销所有旧会话并使旧操作链接失效。邮箱变更由
`POST /internal/v1/auth/email-change/request` 发起，确认接口消费目标邮箱令牌、更新邮箱并撤销所有旧会话；目标邮箱在签发和消费时都会重新检查唯一性。
重置链接默认有效 30 分钟，绑定签发时的密码摘要，不能覆盖签发后发生的密码修改。
Java 在同一事务中锁定账号、验证密码并签发 Session，关闭重置与登录并发窗口。

## 登录会话与修改密码

`/internal/v1/auth/sessions/` 提供 `login`、`resolve`、`logout`、`logout-all`、
`change-password` 五个内部 POST 接口，共用认证开关和内部 Token。
Session 原文只返回给 Python 设置 HttpOnly Cookie，数据库只存 SHA-256 摘要；闲置有效期
7 天，绝对有效期 30 天。解析时顺延闲置期限，但不超过绝对期限。
单设备退出不影响其他设备；全部退出包含当前设备，管理员操作与审计同事务提交。
修改密码校验当前密码，同时撤销所有设备并作废验证/重置链接，失败则整体回滚。
Python 网页 API 的外部 URL 保持不变；混合网关将 `/api/auth/*` 与 `/api/account/password`
直达 Java，其他页面/API 仍由 Python 提供。Java 浏览器控制器完成注册、登录、邮箱验证/变更、
找回密码、当前账号/余额投影、退出和改密，并设置 Session/CSRF Cookie。Java 独立执行这些
路径的安全过滤；Python 保留其余业务接口，经内部 Token 调用同一 Java 认证事实源。
纯 Python 分支及关闭迁移开关的开发拓扑不依赖 Java；本分支尚未切换真实生产流量。

## Java Web 安全基础

本地 Compose 和联合生产 Compose 均启用 `PLATFORM_WEB_SECURITY_ENABLED=true`，
由 `WebSecurityFilter` 保护 Java `/api/*`。浏览器认证经网关直达 Java；内部 `/internal/*`
继续使用服务 Token，不依赖浏览器 Cookie，邮件和账务任务不会被浏览器限流拦截。

- 匿名白名单以外的接口必须通过真实 Java Session 校验，`/api/admin/*` 还要求管理员角色。
  `GET /api/admin/security` 可查看非敏感的安全配置状态，仅允许管理员访问；管理 API 当前未被网关转发到 Java。
- 请求先按直连 IP 限流，已登录账号再按账号限流。Redis TIME + 原子滑动窗口 Lua 保证副本共享窗口，
  429 返回 `Retry-After`；Redis/账号查询故障返回 503，不回退到本地认证或放行。
- Redis 使用同步 Jedis 和有界连接池，连接/读写/池等待均为 1 秒。
- 默认窗口 60 秒，认证 20、管理/写入 120、其他 240 次，可沿用 `JOB_AGENT_RATE_LIMIT_*` 配置。
  Java key 前缀默认 `job_agent:platform_rate_limit`，与 Python 的计数隔离，避免同一请求被重复计数。
- 已携带会话 Cookie 的写请求执行 CSRF 双提交校验，登录/注册豁免；设置安全响应头和 `no-store`。
  生产要求 HTTPS 公网地址、Secure Cookie、CSRF、Redis 密码和至少 32 字符的内部 Token，否则拒绝启动。
- 规范化 `X-Request-ID` / `X-Trace-Id`，日志不写请求正文、密码、Cookie 或账号标识；
  Micrometer 记录 `platform.web.requests` 和 `platform.web.duration`，标签仅用请求组/状态族/结果。
  `/internal/metrics` 使用独立 Bearer 令牌供 Prometheus 抓取，Actuator 不公开 Prometheus 端点。
- `PLATFORM_TRUSTED_PROXY_CIDRS` 默认留空，不信任任何转发头。仅当直连对端属于已配置 IP/CIDR 时，
  才从右向左检查 `X-Forwarded-For`，取首个不可信跳点作为来源；同一地址也用于 Java 登录设备记录。
  IPv4/IPv6 规范化后用于限流，禁止主机名、端口、重复头、超过 16 跳的链和全网信任网段。
  不使用 `Forwarded` 或 `X-Forwarded-Proto` 决定认证/HTTPS，Tomcat 自动转发处理明确关闭。

### Prometheus 抓取

Java+Python Compose 要求 `JOB_AGENT_PLATFORM_METRICS_TOKEN` 为独立的 32 位以上随机令牌，不能复用业务内部 Token。
该令牌通过 `platform_metrics_token` Compose Secret 提供给 Java 和 Prometheus，不写入采集 YAML。
`deploy/prometheus/prometheus.hybrid.yml` 增加 `job-hunting-platform` DNS target，保留 Python target；
生产 Java 端口不映射到公网，Caddy/Nginx 不代理该内部指标路径。
Prometheus 中可查询 `up{job="job-hunting-platform"}`、`platform_web_requests_total` 和
`platform_web_duration_seconds_bucket`。浏览器认证切换到混合网关后，Java 浏览器计数应随认证流量增长；内部认证流量还可查看框架的 `http_server_requests_seconds_count`。

`scripts/validate_python_java_local.py` 会创建临时 Redis 容器和隔离 schema，实测 Java 登录、
管理员权限、CSRF、429、请求 ID、Redis 停机/恢复和生产错误配置拒绝启动，并在结束时移除临时资源。`RedisRequestLimiterIntegrationTest`
通过 Testcontainers 实测多副本并发窗口和恢复；Docker 验收也启用相同安全过滤器。
验收同时检查可信代理下的独立客户端限流、伪造链拦截和指标令牌；Docker 验收经真实
Caddy 网关完成 Java 注册/登录、Python 余额读取和改密后的跨服务会话撤销，并启动 Prometheus
确认 Java target 为 UP。
本地 Docker Hub 不可达时，Docker 验收脚本支持 `--java-jar platform-service/target/platform-service-0.1.0-SNAPSHOT.jar`，
在已有 Java 21 运行镜像中挂载新打包产物；此选项不替代 CI 对正式 Dockerfile 的构建与安全扫描。
管理员账号列表、当前账号读取、显示名称写入、启用/停用、账号注销准入/完成和首次管理员引导现由 Java 原子完成：服务端用真实 Session
重新校验管理员权限，停用同时撤销目标会话、作废旧账号操作链接并写入审计。Python
网页接口只把 Java 的安全账号投影转换回原有字段名，因此前端 URL 不变。账号注销准入和最终匿名化已由
Java 完成，注销后的知识库、对象存储和后台任务清理仍由 Python 的可恢复删除任务状态机负责，
清理失败时账号保持禁用，重试成功后再完成 Java 侧匿名化。
已签发的 Java 验证链接原样保留；旧 Python 操作链接在 Java 模式下不再支持，应重新请求。
Java 模式下不派发旧 Python 邮件任务；关闭迁移开关时仍保留兼容实现，不能称为全部清理。
本地与联合生产部署步骤见 [Python + Java 部署指南](../docs/learning/python-java-deployment.md)。

充值和扣费接口都要求 `Idempotency-Key` 与请求中的 `source_reference` 相同。Java 在充值时同一事务写入 `recharge_orders`、`account_balance_ledger` 和 `payment_events`，重复充值只返回原账务结果，不会重复到账。余额不足时返回 `INSUFFICIENT_BALANCE` 和 `余额不足，请先充值后重试`。余额行使用 PostgreSQL 行锁，账务流水使用唯一约束保证重试不会重复写入。Python 在远端扣费失败时保留 `usage_events`，后续使用同一个 `call_id` 重试，不会重复扣费。

真实支付的第一阶段已经加入：内部 `POST /internal/v1/billing/orders` 只登记待支付订单，不改变余额；渠道适配器将回调规范化为 `POST /api/payments/webhooks/{provider}`，使用 `X-Payment-Timestamp` 和 `X-Payment-Signature: sha256=...` 校验 HMAC-SHA256。回调时间戳默认只接受 5 分钟内的请求，事件按渠道和事件 ID 幂等，成功回调在同一事务中写入充值流水、更新余额、更新订单状态和支付事件。失败、取消和重复事件不会重复到账。密钥由 `PLATFORM_PAYMENT_WEBHOOK_SECRET` 提供，默认关闭，未接入具体支付渠道前不应在生产开启。

模拟充值的管理员退款也已完成：`POST /api/admin/recharge/orders/{orderId}/refund` 只允许对已到账订单执行一次全额退款，要求当前余额仍足以扣回充值金额；退款会原子写入负向 `adjustment` 流水、更新订单为 `refunded`、写入 `payment.refunded` 事件和管理员审计，使用幂等键重复提交不会重复扣款。`GET /api/admin/recharge/reconciliation` 提供充值订单、充值流水和退款冲销流水的一致性检查。两项能力均不调用真实支付渠道，真实支付适配仍保持关闭。

## 迁移规则

个人中心 `GET /api/me/balance` 和 `GET /api/me/recharge/orders` 经混合网关直接到 Java，
响应字段及前端保持不变。Java 从真实 Session 获取账号，不能通过请求中的账号 ID 查询他人流水。
余额摘要、总数和分页在只读 Repeatable Read 事务中读取；每页最多 100 条，按时间和 ID 倒序。
资金事实不采用 Token/工具日志的五页淘汰规则。查询契约见
[`billing-personal.openapi.yaml`](src/main/resources/billing-personal.openapi.yaml)。
Python 在 Java 账务模式下只转发这两项查询，Java 故障返回 503，不回读本地账务表。
价格、低余额阈值和演示充值配置沿用同一组 `JOB_AGENT_BILLING_*` / `JOB_AGENT_DEMO_RECHARGE_*`
环境参数；生产默认关闭演示充值。

`POST /api/me/balance/recharge` 也经网关直达 Java，要求认证和账务模块同时开启。
Java 在事务中重新校验 Session，按服务端配置控制演示充值开关及单笔/累计额度，不接受用户指定账号或额度。
订单、流水、余额和支付事件原子提交；相同账号的相同幂等键和金额重试不会重复到账，金额变化返回 409。
幂等键在 JSON 的 `idempotency_key` 字段传入，缺省生成新操作；重试必须复用该键。
Python Java 模式只转发原会话和表单，失败不执行本地充值；关闭 Java 模式保留原有兼容路径。
本阶段不接入真实支付，不改变管理员人工补款规则。

管理员余额流水、充值订单、支付事件、退款、对账和人工补款公开路径也由混合网关直达 Java。
Java 用真实 Session 校验管理员权限，补款不信任请求中的操作者 ID，余额/流水/审计同事务提交。
补款返回原流水与当前余额；重试仍校验权限且不新增审计。管理员可为自己及其他账号补款，
停用账号收到补款后仍保持停用。查询每页最多 100 条，不自动淘汰资金事实；退款失败不会产生部分账务写入；
契约见 [`billing-admin.openapi.yaml`](src/main/resources/billing-admin.openapi.yaml)。
Python 在 Java 模式下仅转发这些请求，不能回读旧数据或回退本地补款。
工具执行和轨迹写入、后台任务及请求观测仍在 Python。

`GET /api/admin/accounts`、`PATCH /api/admin/accounts/{account_id}/status` 和
`GET /api/admin/audit/events` 也由混合网关直达 Java，启用/停用复用原有策略锁和账号事务。
禁止自停用或移除最后可用管理员；停用会原子撤销会话、作废操作链接并保存审计，启用后不恢复旧会话。
公开账号投影保持 `id` 等原有安全字段，不暴露密码哈希或 Session。审计按时间/ID 倒序，
默认 50 条、最多 200 条，保留空历史引用及请求 ID，读取同表中的 Python/Java 事件且不删除历史。
Python Java 模式只转发这些请求，状态更新会透传规范化请求 ID；Java 故障不回读本地审计。
契约见 [`admin-accounts.openapi.yaml`](src/main/resources/admin-accounts.openapi.yaml)。

`GET /api/admin/usage/summary` 和 `GET /api/admin/usage/events` 由 Java 提供管理员只读查询。
总览、余额状态、工具任务计数、Token 明细与前端字段保持一致；分页默认/最多 100 条。
按账号保留最新 500 条之后才应用筛选，分页总数和明细来自同一 Repeatable Read 快照。
Python 仍负责用量写入及物理清理，Java 不写运维记录，不裁剪资金流水。
混合模式绕过 Python 的旧汇总缓存，Java 直接查询最新已提交数据；暂不引入跨语言共享缓存。
契约见 [`admin-usage.openapi.yaml`](src/main/resources/admin-usage.openapi.yaml)。

工具调用列表和详情 `/api/admin/tools/traces[/{root_request_id}]` 同样经网关直达 Java。
列表只查询 16 个摘要字段，详情才加载 Python 保存的低敏步骤、重试、结果和失败原因；不缓存或重排轨迹。
两者共享每账号最近 500 条的窗口，旧链接无法查询被淘汰任务；列表保留原有分页/可空字段。
Python 保留执行、写入和定期物理清理职责，Java 不写工具表。
契约见 [`admin-tools.openapi.yaml`](src/main/resources/admin-tools.openapi.yaml)。

`GET /api/admin/tasks` 和 `GET /api/tasks/{task_key}` 经网关直达 Java。
前者支持账号、类型、状态过滤及分页，后者始终按当前 Session 账号查询，管理员也不能通过个人接口读取他人任务。
总数与分页使用只读 Repeatable Read 快照，不缓存进度，不查询或返回 payload、幂等键、内部 ID 和 Session。
未知状态返回 400，无效参数返回 422，外部账号任务与不存在任务统一返回 404。
每页默认/最多 100 条；`max_pages=5` 只是兼容 UI 元数据，不代表删除任务或限制全量查询。
Python 仍独占任务投递、重试、认领、进度更新、完成及失败写入，无需数据库结构变更。
Java 模式的 Python 查询入口只转发真实会话，故障不回退本地查询。
契约见 [`task-reads.openapi.yaml`](src/main/resources/task-reads.openapi.yaml)。

管理员重试/探针的公开 POST 入口现也由 Java 接管，校验会话、权限、CSRF、限流及可选 `Idempotency-Key`。
`PLATFORM_TASKS_PYTHON_BASE_URL` 默认 `http://web:8000`，Compose 使用内部服务名；生产拒绝公网目标和携带凭据的 URL。
Java 不写任务表，通过带服务 Token/真实会话的内部命令调用 Python。Python 用命令 ID 实现任务/审计事务和重放保护，
队列未确认返回 503，重试必须复用原幂等键。内部调用不自动重试或跟随重定向，连接/读取超时及响应大小有界。
Python 继续独占状态写入及执行，并由 Beat 补投 pending 命令；没有引入第二套任务状态机或 Java Celery 客户端。
契约见 [`task-commands.openapi.yaml`](src/main/resources/task-commands.openapi.yaml)。

个人中心 `PATCH /api/account/profile` 和 `PATCH /api/account/email` 已由网关直达 Java。
复用真实 Session/CSRF/限流和既有事务服务，不信任 JSON 中的账号 ID、角色或来源地址。
显示名称去除首尾空格，空值可清除；邮箱统一大小写与首尾空格，重复邮箱 409、同邮箱 400、无效字段 422。
邮箱申请只登记 Java outbox，原邮箱在确认前不变；确认后原子变更邮箱并撤销旧会话。冷却期重复申请不新增投递任务。
SMTP 仍由 Python Worker 领取与回报，Python Web 暂停不影响这两个 Java 入口；不代表所有 Python 进程停止后仍能发送邮件。
混合模式下 Python 只转发，Java 故障返回 503，不读旧账号或回退写旧 outbox；无需新增环境变量或数据库迁移。
契约见 [`account-profile.openapi.yaml`](src/main/resources/account-profile.openapi.yaml)。

`GET /api/admin/observability/requests` 由 Java 校验管理员会话并汇总两端进程快照。
只有 `/api/*` 参与 API 合计，`/internal/*` 单独列出；HTML、静态资源、Actuator 不参与该合计。
平均耗时使用总耗时/总请求数，不平均两个服务的平均值。缺失端以 null/不可用表示，不伪造 0。
邮件汇总包含 Java 三类账号邮件和 Python 历史投递记录，字段显式投影、收件地址和错误脱敏。
`PLATFORM_OBSERVABILITY_PYTHON_BASE_URL` 默认 `http://web:8000`，超时和响应体大小有界；
回调使用服务 Token，内部兼容入口额外校验真实管理员 Session，公网不开放内部路径。
快照不缓存、不持久化，服务重启独立清零，不是跨副本累计或历史监控；Prometheus 保持原有口径。
无需数据库迁移，契约见 [`observability.openapi.yaml`](src/main/resources/observability.openapi.yaml)。

- Java 服务和 Python 服务不能同时写同一组账务表。
- Java 账务开关关闭时，`account_balances`、`recharge_orders`、`account_balance_ledger` 仍由 Python 独占写入；开关打开后，余额、模拟充值和模型扣费由 Java 独占写入。
- 跨服务请求必须带幂等键和 trace id。
- 长耗时 AI 任务通过任务 ID 异步交互，不能让 Java 请求线程等待模型完成。
