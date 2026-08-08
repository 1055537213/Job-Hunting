# PostgreSQL、SQLAlchemy 与 Alembic 学习说明

## 这一步完成了什么

项目现在把网页和自动化测试使用的结构化数据统一放入 PostgreSQL 隔离 schema。
旧数据全部是测试数据，因此本次没有实现导入脚本；数据库直接由 Alembic 从空库创建。

初始 revision 为 `20260807_0001`，其中包含：

- 账号、Session、候选人档案、职位、聊天会话和聊天消息。
- 项目经历待确认卡片、简历草稿和简历文件元数据。
- Model Gateway 的追加式 Token 用量流水。
- 长文本来源材料和生产 RAG 使用的 pgvector `rag_chunks` 派生索引。

## 技术栈与选择理由

| 技术 | 作用 | 为什么在此时引入 |
| --- | --- | --- |
| SQLAlchemy 2.x | 用一个 Engine 管理连接、事务和数据库方言差异 | 业务代码只依赖仓储边界，不直接绑定 psycopg 驱动 |
| Alembic | 将 schema 变化保存为版本化 migration | 生产启动不会临时建表，每次改表都可审计和回退 |
| PostgreSQL 16 | 作为结构化事实源和 pgvector 宿主 | 外键、事务、JSONB、检查约束和向量检索都能在同一数据库治理 |
| pgvector | 为 PostgreSQL 提供 `vector` 列和余弦检索 | 生产 RAG 与结构化数据共享同一事务边界、账号隔离和备份策略 |
| Psycopg 3 | SQLAlchemy 对 PostgreSQL 的驱动 | 支持 PostgreSQL 类型，且 SQLAlchemy 2.x 集成稳定 |
| Docker Compose | 先健康检查数据库，再运行迁移，再启动 Web | 不让应用在表还不存在时对外提供服务 |

## 文件职责

```text
database_schema.py
    定义当前目标 schema，供类型比较和新 migration 参考

alembic/versions/20260807_0001_initial_production_schema.py
    冻结的历史 DDL；以后不能靠修改这个文件改变旧版本含义

database_migrations.py
    upgrade_database / downgrade_database / current_database_revision

sqlalchemy_store.py
    SQLAlchemy Engine、事务适配与现有业务仓储接口之间的边界

compose.yaml
    postgres -> migrate -> web 的本地启动顺序
```

`database_schema.py` 和历史 migration 不是同一件事。前者描述“现在希望表是什么样”，
后者记录“某一个版本如何从旧结构变成新结构”。未来新增字段时，应新建 revision，
不能修改 `20260807_0001`。

## 启动与检查

Docker Compose 会在启动时自动执行迁移。日常使用网页时不需要执行任何迁移命令；
只有本机调试 Docker 以外的 Web 启动方式时，才需要在已配置 PostgreSQL URL 的 `.env` 下运行：

```powershell
alembic upgrade head
alembic current
```

`alembic upgrade head` 默认升级到最新 revision，`alembic current` 只读取当前 revision。
网页、Docker Web 和 Alembic 运行入口只接受 PostgreSQL URL；缺失配置或写入其他数据库 URL 时会拒绝启动，
避免用户资料退回本地测试文件。

## 测试数据库隔离

自动化测试也连接 PostgreSQL，但每个 pytest 会话使用随机 schema：

1. Alembic 空库升级测试，快速检查迁移链是否完整。
2. 业务规则测试在同一数据库引擎内隔离数据，避免测试分支和生产分支行为不一致。

测试 schema 会在会话结束时自动删除。真实 Web 由 `JOB_AGENT_DATABASE_URL` 指向 PostgreSQL，
不会创建本地数据库文件或独立向量目录。

## pgvector 当前状态

迁移会执行 `CREATE EXTENSION IF NOT EXISTS vector`，并创建
`rag_chunks.embedding vector`。生产 Web 会自动选择
`PgVectorKnowledgeBase`：`long_texts` 仍是长文本事实源，`rag_chunks` 只保存可重建的分块、
Embedding、模型身份和维度。

当前实现已具备以下保护：

1. 全量重建按账号原子替换，增量写入按稳定 chunk ID upsert，不会重复产生证据。
2. 查询先按账号、Embedding 模型身份和向量维度过滤，再使用 pgvector 余弦距离召回。
3. 删除长文本或候选人时，外键级联会删除对应派生 chunk。
4. 自动化测试与 Web 使用同一 PostgreSQL + pgvector 后端，避免隐藏的回退实现。

因为项目暂时允许更换 Embedding 模型和维度，当前没有建立 HNSW 或 IVFFlat 索引。数据量增长后，
应先固定生产 Embedding 模型与维度，再通过新的 Alembic revision 为对应向量空间建立合适索引。

## 回退原则

`downgrade_database()` 已提供给受控恢复和演练，但不要在生产环境直接执行回退。正确顺序是：

1. 先确认 migration 是否包含破坏性操作。
2. 备份数据库并验证恢复。
3. 停止会写入该库的 Web/Worker。
4. 在维护窗口执行回退并验证 revision。

本项目当前初始 migration 可回退到空库；未来涉及真实用户数据时，回退策略必须随每个
新 revision 单独设计。
