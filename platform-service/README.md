# Job Hunting Platform Service

这是求职助手的 Java 平台服务，当前实现第一条账务垂直链路：余额查询和模型调用扣费。

默认配置不会连接数据库，账务模块必须显式设置 `PLATFORM_BILLING_ENABLED=true` 后才会启用 PostgreSQL。当前版本不会接管生产流量，账务迁移完成前，现有 Python 服务仍是生产事实源。

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

扣费接口要求 `Idempotency-Key` 与请求中的 `source_reference` 相同。余额不足时返回 `INSUFFICIENT_BALANCE` 和 `余额不足，请先充值后重试`。余额行使用 PostgreSQL 行锁，流水使用 `source_reference` 唯一约束保证重试不会重复扣费。Python 在远端扣费失败时保留 `usage_events`，后续使用同一个 `call_id` 重试，不会重复扣费。

## 迁移规则

- Java 服务和 Python 服务不能同时写同一组账务表。
- 账务接口正式实现前，`account_balances`、`recharge_orders`、`account_balance_ledger` 仍由 Python 独占写入。
- 跨服务请求必须带幂等键和 trace id。
- 长耗时 AI 任务通过任务 ID 异步交互，不能让 Java 请求线程等待模型完成。
