# Job Hunting Platform Service

这是求职助手的 Java 平台服务骨架，第一阶段只提供运行入口、健康检查和跨服务契约位置。

当前版本不会读取或写入业务数据库，也不会接管 Python 的账号、账务和 Agent 流程。账务迁移完成前，现有 Python 服务仍是生产事实源。

## 本地运行

需要 JDK 21 或更高版本，以及 Maven 3.9+：

```bash
mvn -B test
mvn -B spring-boot:run
```

启动后检查：

```text
GET http://127.0.0.1:8081/internal/v1/health
GET http://127.0.0.1:8081/internal/v1/version
GET http://127.0.0.1:8081/actuator/health
```

## 迁移规则

- Java 服务和 Python 服务不能同时写同一组账务表。
- 账务接口正式实现前，`account_balances`、`recharge_orders`、`account_balance_ledger` 仍由 Python 独占写入。
- 跨服务请求必须带幂等键和 trace id。
- 长耗时 AI 任务通过任务 ID 异步交互，不能让 Java 请求线程等待模型完成。
