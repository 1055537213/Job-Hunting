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

## 第二阶段交付：凭据校验迁移

- Java `POST /internal/v1/auth/verify-credentials` 负责查询账号、校验密码哈希、账号状态和邮箱验证状态。
- Java 同时兼容现有 Python 写入的 Argon2id 和旧版 scrypt 哈希，不要求批量重置用户密码。
- Python 继续负责注册、邮箱验证、Session Cookie 和登录后的账号读取；Java 只返回已验证的 `account_id`。
- Python 通过 `JOB_AGENT_JAVA_AUTH_ENABLED`、`JOB_AGENT_JAVA_AUTH_BASE_URL` 和
  `JOB_AGENT_JAVA_AUTH_INTERNAL_TOKEN` 开关此迁移，默认关闭。Java 服务对应使用
  `PLATFORM_AUTH_ENABLED=true` 和相同的 `PLATFORM_INTERNAL_TOKEN`。
- Java 认证服务不可用时，Python 返回 503，不会静默回退到 Python 密码校验，避免双写/双事实源造成行为不一致。
- 本地验收脚本会在隔离 PostgreSQL schema 中验证注册账号、Java 凭据校验、Python Session 创建和后续账务请求。
- 注册请求通过 Java 内部接口完成；账号、零余额摘要和协议同意记录在一个事务中写入，Python 只负责邮箱 Outbox 和外部 API 兼容。

## 下一阶段

下一阶段在本地继续迁移注册、邮箱验证、密码重置和 Session 撤销，并补充跨服务集成测试；完成预发布验收前，生产环境暂不打开 `JOB_AGENT_JAVA_AUTH_ENABLED` 或 `JOB_AGENT_JAVA_BILLING_ENABLED`。
