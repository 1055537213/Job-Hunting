# Python + Java 服务边界

## 当前状态

现有 Python 服务是求职助手的唯一生产业务服务，负责 HTTP API、账号、余额、Agent、RAG、文件分析和后台任务。第一阶段新增 Spring Boot 平台服务骨架，但不接管现有数据写入。

## 目标边界

| 模块 | 当前实现 | 目标所有者 |
| --- | --- | --- |
| Agent、RAG、OCR、视觉分析、模型网关 | Python | Python AI 服务 |
| AI 长任务 Worker | Python + Celery | Python AI 服务 |
| 账号、权限、余额、账务、退款 | Python | Java 平台服务，分阶段迁移 |
| 管理员后台业务接口 | Python | Java 平台服务，分阶段迁移 |
| PostgreSQL、Redis、MinIO | 基础设施 | 共享基础设施，按表和接口隔离 |

## 迁移规则

1. 一个事实表只能有一个服务负责写入。
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
- 开关关闭时 Python 保持原有账务写入路径；开关打开后 Java 独占余额、充值订单、余额流水和支付事件的写入权，Python 只调用接口并读取已提交结果。
- CI 的 Python-Java contract check 会验证模拟充值、模型扣费、数据库流水和幂等重试。

## 下一阶段

下一阶段在预发布环境打开开关并验证充值、扣费、故障重试，再迁移退款和管理员补款；生产环境暂不打开 `JOB_AGENT_JAVA_BILLING_ENABLED`。
