# 生产可观测性

## 数据流

生产拓扑把三类数据分开处理：

1. Web 的 `/internal/metrics` 由 Prometheus 拉取，告警规则由 Prometheus 计算。
2. Web、Worker、Beat 和基础设施容器把日志写到标准输出，Alloy 通过只读 Docker socket 采集后发送到 Loki。
3. Web、SQLAlchemy 和 Celery 使用 OpenTelemetry 生成 Trace，通过 OTLP/HTTP 发送给 Alloy，再由 Alloy 批量转发到 Tempo。

Grafana 预置 Prometheus、Loki 和 Tempo 三个数据源。JSON 日志包含有效 span 的 `trace_id`，因此可以从一条错误日志跳转到对应 Trace；Tempo 也可以按 Trace ID 反查 Loki 日志。

## 隐私和基数边界

- HTTP Trace 只记录方法、路由模板、状态码和 request ID，不记录查询参数、请求头或请求正文。
- SQLAlchemy 插件记录参数化 SQL 操作，不记录绑定参数；业务代码仍不得把用户正文拼进 SQL 文本。
- 日志格式化器会遮盖常见密码、Bearer Token、API Key 和带密码 URL。
- Loki 标签只使用 `stack`、Compose `service`、`environment` 和 `level`。request ID、trace ID、账号 ID 和任务 ID只在日志正文中，不作为标签。
- 生产采用父子一致的 10% Trace 采样；一旦入口请求被采样，同一条 SQL/Celery 子链保持一致。故障调查需要更高覆盖时可临时调高，但要先评估存储和隐私影响。

## 保留和故障边界

- Prometheus 默认保留 15 天。
- Loki 通过 Compactor 保留 14 天日志。
- Tempo 保留 7 天 Trace。
- Loki 和 Tempo 当前使用单机本地卷，适合首台单服务器部署。它们不是业务事实源，也不进入 PostgreSQL/MinIO 业务备份。
- 遥测导出失败不会让业务请求失败。相应代价是 Alloy、Loki 或 Tempo 故障期间可能丢失观测数据。
- Alloy 是唯一挂载 Docker socket 的容器。该 socket 即使只读也具有较高宿主机权限，因此 Alloy 不开放公网端口、不运行业务代码，镜像升级前必须经过安全扫描。

## 告警通知

Alertmanager 从运行时 `.env` 生成配置，真实 SMTP 密码不会进入 Git。它复用账号邮件 SMTP 配置，并通过 `JOB_AGENT_ALERT_EMAIL_TO` 指定值班收件人。通知会分组、去重，每四小时重复一次未恢复告警，同时发送恢复通知。

当前告警覆盖 Web 不可用、5xx 比例、平均响应过慢、安全拦截、并发压力、并发保护后端故障和容量饱和。SMTP 本身故障时邮件无法送达，因此真实上线后仍应补一个独立于本机和 SMTP 的外部可用性探针。

## 访问方式

Grafana、Prometheus 和 Alertmanager 只绑定服务器 `127.0.0.1`。在运维电脑建立隧道：

```powershell
ssh `
  -L 3000:127.0.0.1:3000 `
  -L 9090:127.0.0.1:9090 `
  -L 9093:127.0.0.1:9093 `
  <server-user>@<server-host>
```

随后访问：

- `http://127.0.0.1:3000`：Grafana 指标、日志和 Trace。
- `http://127.0.0.1:9090/targets`：Prometheus 采集目标。
- `http://127.0.0.1:9090/alerts`：告警规则状态。
- `http://127.0.0.1:9093`：Alertmanager 分组与通知状态。

## 配置门禁

CI 使用组件官方命令校验所有配置：Prometheus `promtool`、Alertmanager `amtool`、Alloy `validate`、Loki `-verify-config` 和 Tempo `-config.verify`。生产 SMTP 配置由 `job_hunting_agent.observability_config` 在容器启动时生成；生成失败会阻止 Alertmanager 启动，不会退回仓库内的样例密码。
