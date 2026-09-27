# Java 平台服务发布

当前 `java-platform-migration` 分支将 Python AI 服务和 Java 平台服务作为两个独立镜像发布。Java 服务只加入现有生产网络，不绑定公网端口，也不会停止或修改同机的其他项目。

## 发布产物

推送 `java-platform-migration` 后，CI 先完成 Python、Java、PostgreSQL 联调和安全检查。`publish-platform` job 成功后会发布不可变镜像：

```text
ghcr.io/<owner>/job-hunting-platform:sha-<提交前 12 位>
```

部署前必须确认镜像标签对应的完整提交，并使用同一提交目录中的 `compose.platform.prod.yaml` 和 `scripts/deploy_platform_service.sh`。

## 服务器首次部署

生产 Python Compose 已经创建 `job-hunting-agent-production_default` 网络后，准备 Java 发布目录：

```bash
release_id=sha-<提交前12位>
mkdir -p /opt/job-hunting-agent/platform-releases/$release_id
cp compose.platform.prod.yaml /opt/job-hunting-agent/platform-releases/$release_id/
cp scripts/deploy_platform_service.sh /opt/job-hunting-agent/platform-releases/$release_id/
chmod 700 /opt/job-hunting-agent/platform-releases/$release_id/deploy_platform_service.sh
```

把共享生产 `.env` 中的 `JOB_AGENT_PLATFORM_IMAGE` 改为已验证的 Java 镜像，并保留：

```dotenv
JOB_AGENT_JAVA_BILLING_ENABLED=false
```

然后执行：

```bash
/opt/job-hunting-agent/platform-releases/$release_id/deploy_platform_service.sh \
  /opt/job-hunting-agent \
  "$release_id" \
  "$(grep '^JOB_AGENT_PLATFORM_IMAGE=' /opt/job-hunting-agent/shared/.env | cut -d= -f2-)"
```

脚本只会启动或回滚 `platform-service`。失败时不会执行 `docker compose down -v`，也不会触碰 Python 服务、数据库卷或旧项目。

## 开启 Java 账务前

先在预发布环境确认充值、扣费、重复请求、余额不足、Java 宕机重试和回滚。只有确认 Python Web/Worker 与 Java 使用同一个 PostgreSQL、内部 Token 和价格配置后，才可以同时设置：

```dotenv
JOB_AGENT_JAVA_BILLING_ENABLED=true
PLATFORM_BILLING_ENABLED=true
```

生产切换前必须先完成退款、管理员补款和对账迁移；当前版本只支持 Java 余额查询、模拟充值和模型调用扣费，默认不打开账务切换。
