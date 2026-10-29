# AI 求职准备工作台

Python + Java 多账号 AI 工作台，用于整理候选人档案、职位信息、项目证据、简历材料和对话记忆，并通过 Agent、RAG 与后台任务生成可追溯的求职辅助结果。

系统只处理用户主动提供或明确授权的内容，不登录招聘平台、不抓取隐藏接口、不自动投递，也不自动向招聘方发送消息。

## 1. 项目简介

本项目采用 Java Spring Boot 与 Python FastAPI 协同架构：

- Java 负责账号、Session、权限、CSRF、账务边界和公开 API 网关。
- Python 负责 Agent、RAG、文件分析、职位解析、简历生成和后台任务。
- PostgreSQL/pgvector 保存结构化事实、任务、账务、审计、用量和向量。
- Redis 负责任务队列、共享限流、并发租约和可重建缓存。
- MinIO/S3 保存简历、导出文件、项目原件和视觉派生对象。
- Caddy 或 Nginx 按公开路径分流 Java 与 Python 服务。

项目重点解决求职资料分散、来源难追踪、事实容易污染和长任务难恢复等问题。

## 2. 在线预览 / 效果范围

当前提供本地 Docker 运行方式，不在文档中绑定具体公网地址、账号或运行数据。

| 页面 | 地址 | 内容 |
| --- | --- | --- |
| 登录/注册 | http://127.0.0.1:8000/login | 注册、登录、邮箱验证和密码找回 |
| 工作台 | http://127.0.0.1:8000/ | 档案、职位、项目、对话和简历 |
| 个人中心 | http://127.0.0.1:8000/profile | 余额、演示充值、消费流水和账号安全 |
| 管理后台 | http://127.0.0.1:8000/admin | 账号、用量、请求观测、审计和工具轨迹 |
| API 文档 | http://127.0.0.1:8000/docs | Swagger/OpenAPI 文档 |
| 健康检查 | http://127.0.0.1:8000/api/health | 数据库、队列、对象存储和模型状态 |

## 3. 功能清单

- 多账号隔离、Session Cookie、CSRF、限流、安全响应头和管理员审计。
- 候选人档案、对话记忆、SSE 流式回复和 Agent 工具调用。
- 职位文本/截图导入、标准化解析、重复检测、技能要求修正和删除。
- GitHub 项目分析、本地项目清单预扫描、分批上传、用户确认和知识库索引。
- PDF、图片、表格、DOCX、PPTX、OCR、视觉分析和来源定位。
- 简历导入、简历生成、项目经历表达和后台任务处理。
- Token 用量、余额、演示充值、管理员补款、账务流水和分页明细。
- Prometheus、Alertmanager、Grafana、Loki、Tempo、OpenTelemetry 和 ClamAV 配置。
- PostgreSQL、对象存储、Worker 恢复、备份和安全验收脚本。

## 4. 技术栈

### 后端

Python 3.12、FastAPI、Uvicorn、SQLAlchemy、Alembic、LangChain、LangGraph、Celery、Redis、PostgreSQL 16、pgvector、MinIO/S3、Java 21、Spring Boot 4、Spring JDBC、Maven。

### 前端

Vue 3 Global Build、HTML/CSS/JavaScript、SSE。页面路径为 /login、/、/profile、/admin。

### 运维与质量

Docker Compose、Caddy/Nginx、Prometheus、Alertmanager、Grafana、Loki、Tempo、OpenTelemetry、Pytest、Ruff、Node 前端测试、Maven 测试、Trivy、OSV Scanner 和 SBOM。

## 5. 项目亮点

1. 事实与推断分离，项目摘要和简历表达经过用户确认后才进入正式事实链路。
2. Java 承载稳定平台边界，Python 承载 AI、RAG、解析和异步业务。
3. 数据库、对象键、RAG、任务、余额和审计全链路校验账号归属。
4. 任务、充值和扣费使用幂等键，支持有限重试、恢复和回滚。
5. PDF、图片、表格和项目文件保留来源定位，支持多模态证据链。
6. Retriever 先取 Top-K，再由 Reranker 取 Top-N；当前默认 K=10、N=5。
7. Redis 只保存可重建结果，业务事实保存在 PostgreSQL。
8. 日志和指标避免记录 Cookie、API Key、提示词和用户文件原文。
9. CI 通过后发布精确提交镜像，支持人工批准和失败回滚。

## 6. 目录结构说明

    src/job_hunting_agent/       Python Web、Agent、RAG、存储和任务
    platform-service/            Spring Boot Java 平台服务
    alembic/                     数据库迁移
    tests/                       Python、Java 契约和前端回归测试
    evals/rag/                   RAG 评测集和困难负样本
    scripts/                     本地、生产、恢复和安全验收脚本
    deploy/                      HTTPS、指标、日志、Trace 和告警配置
    docs/                        架构、部署、恢复和安全文档
    compose.yaml                 基础开发拓扑
    compose.platform.yaml        Java 本地联调覆盖
    compose.prod.yaml            单机生产覆盖
    compose.hybrid.prod.yaml     Python + Java 生产覆盖
    Dockerfile                   Python Web/Worker/Beat/Migrate 镜像
    platform-service/pom.xml     Java Maven 依赖和测试配置
    .env.example                 脱敏配置模板

## 7. 运行步骤

### 环境要求

Docker Desktop 或 Docker Engine + Compose v2、Python 3.12、Node.js 22、JDK 21、Maven 3.9+，以及 OpenAI-compatible Chat/Embedding 模型。

### 获取代码和配置

    git clone <repository-url>
    Set-Location <project-directory>
    Copy-Item .env.example .env

在 .env 中填写模型、对象存储、Redis、数据库和平台服务配置。混合模式至少需要：

    JOB_AGENT_PLATFORM_INTERNAL_TOKEN=<随机生成的服务密钥，至少32字符>
    JOB_AGENT_JAVA_AUTH_ENABLED=true
    JOB_AGENT_JAVA_BILLING_ENABLED=false

不要提交 .env、生产密钥、邮箱密码、模型 API Key、数据库导出或备份。

### 启动本地拓扑

    docker compose -f compose.yaml -f compose.platform.yaml up -d --build
    docker compose -f compose.yaml -f compose.platform.yaml ps

默认访问 http://127.0.0.1:8000/。本地浏览器分流可使用 hybrid-local profile：

    $env:JOB_AGENT_BROWSER_BASE_URL = 'http://localhost:18082'
    docker compose -f compose.yaml -f compose.platform.yaml --profile hybrid-local up -d --build

停止服务但保留数据卷：

    docker compose -f compose.yaml -f compose.platform.yaml down

### 质量检查

    ruff check src tests alembic
    python -m compileall -q src tests alembic
    python -m pytest -q
    Get-ChildItem tests -Filter 'frontend_*.mjs' | ForEach-Object { node $_.FullName }
    Set-Location platform-service
    mvn -B -ntp test

生产 Compose 应使用受限环境变量文件和不可变镜像，并在部署前完成备份、人工批准和健康检查。具体步骤见 docs/learning/python-java-deployment.md。

## 8. 接口文档

启动 Web 后，以 /docs 和 /redoc 为准。常用接口：

| 模块 | 方法与路径 | 说明 |
| --- | --- | --- |
| 认证 | POST /api/auth/register | 注册账号 |
| 认证 | POST /api/auth/login | 登录并设置 Session |
| 认证 | POST /api/auth/verify-email | 消费邮箱验证令牌 |
| 账号 | GET /api/auth/me | 当前账号与余额摘要 |
| 账号 | PATCH /api/account/profile | 修改账号显示名称 |
| 账号 | GET /api/account/export | 导出本人数据 |
| 账号 | POST /api/account/delete | 发起账号注销 |
| 档案 | GET/POST/DELETE /api/profiles | 查询、创建和删除档案 |
| 对话 | POST /api/chat | 普通 Agent 对话 |
| 对话 | POST /api/chat/stream | SSE 流式对话 |
| 职位 | POST/GET /api/jobs | 导入和查询职位 |
| 职位 | PUT /api/jobs/{id}/skill-requirements | 修正职位技能分类 |
| 职位 | DELETE /api/jobs/{id} | 删除职位 |
| 项目 | GET /api/projects | 查询项目卡片 |
| 项目 | POST /api/projects/{id}/confirm | 确认项目卡片 |
| 项目 | DELETE /api/projects/{id} | 删除项目及关联知识 |
| 简历 | POST /api/resumes/upload | 上传和解析简历 |
| 任务 | GET /api/tasks/{task_key} | 查询后台任务 |
| 计费 | GET /api/me/balance | 查询余额和消费摘要 |
| RAG | GET /api/rag/search | 账号隔离检索 |
| 运维 | GET /api/health | 应用健康检查 |
| 监控 | GET /internal/metrics | Prometheus 内部指标 |

写操作需要同源 Cookie 和 X-CSRF-Token；管理接口需要管理员角色；内部接口不应暴露到公网。

## 9. 常见问题（FAQ）

### Java 和 Python 如何分工？

Java 承载账号、Session、权限、CSRF、账务和公开边界；Python 承载 Agent、RAG、文件解析、多模态处理、简历生成、工具执行和后台任务。两端通过服务令牌和真实 Session 的内部回调通信。

### Java 服务不可用时会绕过权限吗？

不会。需要 Java 认证或账务的入口会失败关闭，避免权限绕过、双事实源或旧余额。

### RAG 默认 Top-K 和 Top-N 是多少？

Retriever Top-K=10，Reranker Top-N=5。正式上线前应使用真实业务评测集、困难负样本和 P95 延迟重新校准。

### 模拟充值是真实支付吗？

不是。模拟充值用于本地演示和测试。具体支付渠道、退款状态机和渠道对账需要单独接入并验收。

### Redis 故障会导致事实丢失吗？

不会。账号、任务、账务和审计事实保存在 PostgreSQL；Redis 负责队列、限流、并发租约和可重建缓存。

### 项目会自动登录招聘平台或自动投递吗？

不会。项目只处理用户主动提供的职位和材料，不运行招聘平台自动化，也不发送未经确认的招聘消息。

## 10. TODO / 未来计划

- [x] Python + Java 本地联调、内部契约、CI 和镜像发布流程。
- [x] 账号、Session、权限、账务、审计、用量和请求观测边界。
- [x] 档案、聊天、项目、简历和职位入口的 Java 网关接入。
- [x] 任务可靠投递、幂等、重试、回收和账号删除清理链路。
- [x] ClamAV、备份恢复、Prometheus、日志和 Trace 配置及验收脚本。
- [ ] 接入具体支付渠道、签名 Webhook、退款状态机和渠道对账。
- [ ] 使用真实行业材料完成 RAG 发布集、Top-K/Top-N 和 P95 性能验收。
- [ ] 增强工业 PDF 表格/图注坐标、父子 Chunk、数值范围检索和 CAD 解析能力。
- [ ] 完成目标服务器的首次生产部署、HTTPS 验收、恢复演练和容量基线。
- [ ] 持续执行密钥轮换、依赖升级、渗透测试和故障演练。

## 11. 联系方式 / 声明

- 项目问题：请通过项目托管平台的 Issue 页面反馈。
- 架构边界：docs/architecture/python-java-boundary.md
- 部署指南：docs/learning/python-java-deployment.md
- 发布与恢复：docs/learning/production-release.md
- 本地联调：scripts/validate_python_java_local.py

本项目用于求职准备、信息整理和材料生成辅助，不保证职位匹配、面试或录用结果。用户应对提交内容的真实性、隐私授权和最终发送行为负责。
