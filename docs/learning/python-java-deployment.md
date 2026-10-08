# Python + Java 本地运行与部署

## 当前边界

网页入口仍是 Python；Java 负责账号注册、密码校验、邮箱验证、密码重置和可选账务。
Java 使用 Spring Boot 4.0 / Java 21，JSON 序列化使用 Jackson 3；
本地与 CI 使用同一 `pom.xml` 和 Python 3.12 锁文件，不需要服务器专用代码。
Python 的 SMTP/Celery Worker 通过 Java 内部接口投递验证/重置邮件，不写 Java 邮件状态。
Java 验证令牌只保存 SHA-256 摘要，由服务端密钥重建邮件 URL；投递回报必须带本次认领键。
SMTP 不支持 exactly-once：发送成功后 Worker 失联仍可能重复发送，但令牌只能消费一次。
新注册的待验证账号与邮件任务原子创建，Beat 默认每 30 秒发现到期任务。
开关切换后旧 Python 验证/重置链接不再有效，应重新发送，不自动回退。
已有 Java 验证链接在账本升级后保持有效。重置默认 30 分钟过期，可用
`JOB_AGENT_PASSWORD_RESET_TOKEN_TTL_MINUTES` 配置；账号和来源限额按用途分别统计。
重置成功原子撤销旧 Session，并作废所有旧操作链接；签发后密码变化也会让该重置链接失效。
Python 在创建 Session 的事务里锁定账号并验证认证前的密码快照，防止旧密码并发登录绕过撤销。
修改密码、注销、管理员初始化及 Session 的其余操作暂留 Python，后续继续迁移。

## 环境配置

沿用现有 `.env` 的数据库、对象存储、Redis、模型和 SMTP 配置，补充：

```ini
JOB_AGENT_PLATFORM_INTERNAL_TOKEN=<随机生成至少32字符的服务间密钥>
JOB_AGENT_JAVA_AUTH_ENABLED=true
JOB_AGENT_JAVA_BILLING_ENABLED=true
JOB_AGENT_ACCOUNT_ACTION_SECRET=<随机生成至少32字符的令牌派生密钥>
JOB_AGENT_PUBLIC_BASE_URL=http://localhost:8000
JOB_AGENT_EMAIL_VERIFICATION_REQUIRED=true
```

Compose 会把同一个内部 Token 分别注入 Python 认证/账务客户端和 Java，无需重复填写三份。
生产把 `JOB_AGENT_PUBLIC_BASE_URL` 改为用户实际访问的 HTTPS 地址，并保持既有生产密钥配置。
不要把密钥填进仓库示例、命令行或 GitHub 提交。配置差异是环境参数，不是代码修改。

## 本地

```powershell
docker compose -f compose.yaml -f compose.platform.yaml up -d --build
.\scripts\validate_python_java_local.ps1
```

验收脚本使用随机 PostgreSQL schema，结束后删除自己的测试 schema，不清空现有用户数据。
脚本使用真实 Python Web/Java 进程和 Docker PostgreSQL验证注册、邮箱验证、密码重置、旧会话撤销、重复确认、登录、
模拟充值及扣费；测试发送器捕获邮件，不向真实收件人发送。
CI 还构建两份镜像，运行 `scripts/validate_python_java_docker.py` 验证容器间相同链路。
该验收使用测试发送器，不等同于真实 SMTP/完整 Worker 队列验收。
Java 默认测试不启动 PostgreSQL 容器，完整集成测试：

```powershell
cd platform-service
mvn -B -ntp "-Dtest=BillingServiceIntegrationTest,AccountActionEmailIntegrationTest" "-Drun.integration.tests=true" test
```

## 生产指令

CI 为 `java-platform-migration` 的同一个完整 SHA 发布两份独立镜像：
`ghcr.io/1055537213/job-hunting-ai:sha-<前12位>` 与
`ghcr.io/1055537213/job-hunting-platform:sha-<前12位>`，不覆盖纯 Python 镜像。

首次部署需要先准备服务器 Docker、`<app-root>/shared/.env`、镜像仓库拉取权限，以及 HTTPS/证书。
在 GitHub `Deploy Python Java` 选择完整 SHA、拓扑，输入 `DEPLOY`，并审批 production 环境。
工作流核对精确提交的 CI 成功、两个镜像标签及 revision 一致，然后上传部署文件，执行：

```bash
bash <release-dir>/scripts/deploy_production.sh \
  <app-root> sha-<前12位> \
  ghcr.io/1055537213/job-hunting-ai:sha-<前12位> coexist \
  ghcr.io/1055537213/job-hunting-platform:sha-<前12位>
```

手动执行前需拉取两份镜像，并把该提交的 `compose.yaml`、`compose.prod.yaml`、
`compose.coexist.yaml`、`compose.platform.yaml`、`compose.hybrid.prod.yaml`、`deploy/`、`scripts/`
放到 `<app-root>/releases/sha-<前12位>/`；工作流自动完成这些准备。
服务器不构建镜像，不修改任何项目源码。流程先备份数据库、完成迁移，再等待 Java 健康，
然后启动 Web/Worker/Beat；失败会尝试恢复此前的 Python/Java 镜像组合，数据库迁移不逆转。
共存模式额外消耗 Java 的内存，部署前需确认服务器资源足够；不会停止旧项目。
备份、恢复和 HTTPS 重载脚本会读取 `state/current-platform-image`，自动加载同一联合配置；
备份期间暂停 Java 写入，恢复时重新启动 Java。不能手动删掉该状态文件。

这些步骤只建立可部署能力，不代表已经做过真实服务器演练；本分支暂不部署。
