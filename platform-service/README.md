# Job Hunting Platform Service

这是求职助手的 Java 平台服务，当前实现账务垂直链路，以及账号注册、凭据校验、邮箱验证、密码重置和会话管理内部接口。

运行基线为 Spring Boot 4.0、Spring Framework 7、Jackson 3、Tomcat 11 和 Java 21。
依赖版本由 Maven 锁定，Tomcat/Jackson 的安全修复版本在 `pom.xml` 中显式覆盖；
CI 会扫描传递依赖，不通过忽略漏洞绕过门禁。

默认配置不会连接数据库，账务或认证模块必须显式设置 `PLATFORM_BILLING_ENABLED=true` 或 `PLATFORM_AUTH_ENABLED=true` 后才会启用 PostgreSQL。启用迁移模块后，Java 接管对应写入，Python 仍是网页入口并负责 SMTP/后台任务。本分支不接管现有生产流量。

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

邮箱验证和密码重置共用 Java 独占的 `platform_account_action_emails` 账本；Python SMTP Worker
通过内部 API 认领并回报投递，不直接更新验证状态。一次性令牌只保存摘要，认领键
阻止失联 Worker 的迟到结果覆盖新任务状态。`POST /internal/v1/auth/password-reset/confirm`
在一个事务里消费令牌、更新 Argon2id 密码、撤销所有旧会话并使旧操作链接失效。
重置链接默认有效 30 分钟，绑定签发时的密码摘要，不能覆盖签发后发生的密码修改。
Java 在同一事务中锁定账号、验证密码并签发 Session，关闭重置与登录并发窗口。

## 登录会话与修改密码

`/internal/v1/auth/sessions/` 提供 `login`、`resolve`、`logout`、`logout-all`、
`change-password` 五个内部 POST 接口，共用认证开关和内部 Token。
Session 原文只返回给 Python 设置 HttpOnly Cookie，数据库只存 SHA-256 摘要；闲置有效期
7 天，绝对有效期 30 天。解析时顺延闲置期限，但不超过绝对期限。
单设备退出不影响其他设备；全部退出包含当前设备，管理员操作与审计同事务提交。
修改密码校验当前密码，同时撤销所有设备并作废验证/重置链接，失败则整体回滚。
Python 网页 API 保持不变；Java 模式不再通过 Python 创建、解析、续期或撤销用户登录会话。
管理员账号列表、当前账号读取、显示名称写入、启用/停用和首次管理员引导现由 Java 原子完成：服务端用真实 Session
重新校验管理员权限，停用同时撤销目标会话、作废旧账号操作链接并写入审计。Python
网页接口只把 Java 的安全账号投影转换回原有字段名，因此前端 URL 不变。账号注销准入已由
Java 完成，注销后的账号、知识库、对象存储和后台任务清理仍由 Python 的可恢复删除任务
状态机负责，避免同步清理失败后留下半删除账号。
已签发的 Java 验证链接原样保留；旧 Python 操作链接在 Java 模式下不再支持，应重新请求。
Java 模式下不派发旧 Python 邮件任务；关闭迁移开关时仍保留兼容实现，不能称为全部清理。
本地与联合生产部署步骤见 [Python + Java 部署指南](../docs/learning/python-java-deployment.md)。

充值和扣费接口都要求 `Idempotency-Key` 与请求中的 `source_reference` 相同。Java 在充值时同一事务写入 `recharge_orders`、`account_balance_ledger` 和 `payment_events`，重复充值只返回原账务结果，不会重复到账。余额不足时返回 `INSUFFICIENT_BALANCE` 和 `余额不足，请先充值后重试`。余额行使用 PostgreSQL 行锁，账务流水使用唯一约束保证重试不会重复写入。Python 在远端扣费失败时保留 `usage_events`，后续使用同一个 `call_id` 重试，不会重复扣费。

## 迁移规则

- Java 服务和 Python 服务不能同时写同一组账务表。
- Java 账务开关关闭时，`account_balances`、`recharge_orders`、`account_balance_ledger` 仍由 Python 独占写入；开关打开后，余额、模拟充值和模型扣费由 Java 独占写入。
- 跨服务请求必须带幂等键和 trace id。
- 长耗时 AI 任务通过任务 ID 异步交互，不能让 Java 请求线程等待模型完成。
