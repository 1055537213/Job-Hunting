# Hybrid fact source and semantic index

我们将学历、经验年限、技能、证书、偏好和职位筛选字段保存为 PostgreSQL 结构化事实源，将项目描述、成果材料、职位全文和对话上下文登记到 PostgreSQL 的 `long_texts`，再由 pgvector 生成 `rag_chunks` 派生索引。这样保留精确过滤、范围查询、更新、审计和个人信息生命周期管理能力，同时利用向量检索处理长文本语义匹配；向量记录必须保留来源标识，不能成为唯一事实源。
