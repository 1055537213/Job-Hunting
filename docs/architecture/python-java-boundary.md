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
- 当前 Python 账务实现保持唯一写入权，避免双写和重复扣费。

## 下一阶段

实现 Java 账务模块并接入 PostgreSQL。完成双服务集成测试后，再让 Python `model_gateway` 通过内部接口调用 Java 的扣费接口，最后迁移充值、退款和管理员补款。
