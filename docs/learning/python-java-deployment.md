# Python + Java 本地运行与部署

## 当前边界

混合部署通过 Caddy/Nginx 分流：`/api/auth/*` 和 `/api/account/password` 直达 Java；
页面及其他 API 仍到 Python。Java 负责账号注册、密码校验、邮箱验证、密码重置、
登录会话、退出、修改密码和可选账务。
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
Java 在同一事务里锁定账号、校验密码和创建 Session，防止旧密码并发登录绕过撤销。
Java 解析会话、处理单设备/全部退出及修改密码，并为直达 Java 的浏览器请求设置 Cookie/CSRF；
Python 上尚未迁移的账号 API 仍通过 Java 内部接口解析同一 Session。
Java 中断时受保护接口返回 503，不将故障伪装成未登录，也不回退本地认证。
管理员禁用及管理员初始化已由 Java 负责；账号注销的最终数据清理仍由 Python Worker
执行，但注销准入、Java Session 撤销和操作链接失效由 Java 原子完成。Python 只保存
受控对象键并登记可恢复的 `account_deletion` 任务；对象存储故障不会恢复账号为 active，
任务保留在队列中等待重试。

## 环境配置

沿用现有 `.env` 的数据库、对象存储、Redis、模型和 SMTP 配置，补充：

```ini
JOB_AGENT_PLATFORM_INTERNAL_TOKEN=<随机生成至少32字符的服务间密钥>
JOB_AGENT_PLATFORM_METRICS_TOKEN=<另一个随机生成至少32字符的监控密钥>
# 生产填实际受控 Caddy/Nginx 容器的精确 IP/CIDR，不能填全网范围。
PLATFORM_TRUSTED_PROXY_CIDRS=<受控网关容器所在网络的CIDR>
JOB_AGENT_JAVA_AUTH_ENABLED=true
JOB_AGENT_JAVA_BILLING_ENABLED=true
JOB_AGENT_ACCOUNT_ACTION_SECRET=<随机生成至少32字符的令牌派生密钥>
JOB_AGENT_PUBLIC_BASE_URL=http://localhost:8000
JOB_AGENT_EMAIL_VERIFICATION_REQUIRED=true
JOB_AGENT_CONSENT_REQUIRED=true
JOB_AGENT_TERMS_VERSION=<当前服务条款版本>
JOB_AGENT_PRIVACY_VERSION=<当前隐私政策版本>
```

Compose 会把同一个内部 Token 分别注入 Python 认证/账务客户端和 Java，无需重复填写三份。
生产把 `JOB_AGENT_PUBLIC_BASE_URL` 改为用户实际访问的 HTTPS 地址，并保持既有生产密钥配置。
不要把密钥填进仓库示例、命令行或 GitHub 提交。配置差异是环境参数，不是代码修改。

## 本地

```powershell
docker compose -f compose.yaml -f compose.platform.yaml up -d --build
.\scripts\validate_python_java_local.ps1
```

默认本地页面仍由 `http://127.0.0.1:8000` 直接提供，邮件链接也指向该端口。
要通过与生产一致的混合网关验收浏览器认证，先把 `.env` 中
`JOB_AGENT_BROWSER_BASE_URL` 设为 `http://localhost:18082`，然后执行：

```powershell
docker compose -f compose.yaml -f compose.platform.yaml --profile hybrid-local up -d --build
```

浏览器使用 `http://127.0.0.1:18082/login`。修改该地址后需重新创建
`platform-service`，确保验证/重置邮件链接指向网关。网关只监听本机环回地址；
本地访问 Java 的 `/internal/*` 返回 404，Python/Java 的内部服务调用仍走容器网络。

验收脚本使用随机 PostgreSQL schema，结束后删除自己的测试 schema，不清空现有用户数据。
脚本使用真实 Python Web/Java 进程和 Docker PostgreSQL 验证注册、邮箱验证、密码重置、旧会话撤销、重复确认、登录、
模拟充值及扣费；测试发送器捕获邮件，不向真实收件人发送。
CI 还构建两份镜像，运行 `scripts/validate_python_java_docker.py` 验证容器间相同链路、
浏览器经网关走 Java 认证与 Python 余额接口，以及改密后的跨服务会话撤销。
Docker 验收还运行真实 Prometheus，验证独立凭据和 Java target UP；本地 Prometheus 默认在
`http://127.0.0.1:9090`，可查询 `up{job="job-hunting-platform"}`。不要把监控令牌提交到 Git。
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
`compose.coexist.yaml`、`compose.platform.yaml`、`compose.hybrid.prod.yaml`、
`compose.hybrid.coexist.yaml`、`deploy/`、`scripts/`
放到 `<app-root>/releases/sha-<前12位>/`；工作流自动完成这些准备。
服务器不构建镜像，不修改任何项目源码。流程先备份数据库、完成迁移，再等待 Java 健康，
然后启动 Web/Worker/Beat；失败会尝试恢复此前的 Python/Java 镜像组合，数据库迁移不逆转。
共存模式额外消耗 Java 的内存，部署前需确认服务器资源足够；不会停止旧项目。
备份、恢复和 HTTPS 重载脚本会读取 `state/current-platform-image`，自动加载同一联合配置；
备份期间暂停 Java 写入，恢复时重新启动 Java。不能手动删掉该状态文件。
独立拓扑使用 `deploy/Caddyfile.hybrid`，共存拓扑使用
`deploy/nginx/coexist-hybrid-ip-https.conf.template`；两者均只代理公开认证路径至 Java，
拒绝公网 `/internal/*`，并将其他流量转至 Python。Java 端口只在容器网络内暴露。

这些步骤只建立可部署能力，不代表已经做过真实服务器演练；本分支暂不部署。
