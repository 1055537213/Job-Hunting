"""项目核心数据模型。

这里集中定义跨模块传递的数据结构。模型尽量保持“只描述业务事实”，
不混入数据库连接、命令行输入或 LLM 调用细节，方便后续替换底层实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 动态匹配的默认权重；用户在对话中明确表达优先级后，按字段覆盖。
DEFAULT_PREFERENCE_WEIGHTS = {
    "city": 1.0,
    "salary": 1.0,
    "skills": 1.0,
    "direction": 1.0,
    "experience": 1.0,
}


def sanitize_preference_weights(values: dict[str, float] | None) -> dict[str, float]:
    """只保留支持的维度，并归一化到约定的 1.0/1.5/2.0。"""

    result = dict(DEFAULT_PREFERENCE_WEIGHTS)
    allowed_weights = (1.0, 1.5, 2.0)
    for key, value in (values or {}).items():
        if key not in result:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        result[key] = min(allowed_weights, key=lambda allowed: abs(allowed - numeric))
    return result


@dataclass
class CandidateProfileInput:
    """创建候选人档案时需要的输入数据。

    这里刻意只放结构化事实和偏好，长文本材料会通过单独的
    `long_texts` 表保存，后续可替换成向量检索索引。
    """

    name: str
    status: str
    education: str
    experience_years: float
    skills: dict[str, str]
    preferred_cities: list[str]
    salary_floor_k: int | None
    expected_salary_k: int | None
    target_directions: list[str]
    unacceptable: list[str] = field(default_factory=list)
    # 首选城市和其他可接受城市分开保存，便于匹配器区分优先级。
    acceptable_cities: list[str] = field(default_factory=list)
    # 用户在对话中表达的长期排序偏好，例如 city=2.0、salary=1.5。
    preference_weights: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_PREFERENCE_WEIGHTS)
    )


@dataclass
class CandidateProfile(CandidateProfileInput):
    """已经写入结构化事实源后的候选人档案。

    与 `CandidateProfileInput` 相比，它多了数据库分配的 `id`。
    """

    id: int = 0


@dataclass
class SkillRequirement:
    """职位技能及其重要性分类。"""

    name: str
    category: str
    confidence: float
    evidence: str = ""


@dataclass
class ImportedJob:
    """标准化后的职位信息。

    `raw_text` 保存候选人主动导入的职位原文；其他字段是系统解析出的
    可比较结构化字段。`field_confidence` 和 `uncertainty_notes` 用来避免
    把不完整职位伪装成完整职位详情。
    """

    id: int
    raw_text: str
    source_url: str | None
    title: str
    city: str | None
    salary_min_k: int | None
    salary_max_k: int | None
    salary_months: int | None
    salary_unit: str
    experience_min_years: float | None
    experience_max_years: float | None
    experience_label: str | None
    education: str | None
    company_name: str | None
    industry: str | None
    company_size: str | None
    skills: list[str]
    description_text: str
    field_confidence: dict[str, float]
    uncertainty_notes: list[str]
    # 由规则或 LLM 分类后的技能要求；旧职位没有该字段时使用规则回退。
    skill_requirements: list[SkillRequirement] = field(default_factory=list)
    # 用户提供职位内容的方式与服务端实际接收时间，用于来源追溯；不会读取来源链接。
    import_method: str = "text"
    captured_at: str | None = None


@dataclass
class MatchResult:
    """职位匹配结果。

    匹配结果不是录用概率，而是可解释的推荐判断：包含分数、推荐档位、
    硬性淘汰原因、扣分项、风险和简历优化方向。
    """

    job_id: int
    candidate_id: int
    score: int
    tier: str
    eliminated: bool
    reasons: list[str]
    elimination_reasons: list[str]
    deductions: list[str]
    risks: list[str]
    uncertainty_notes: list[str]
    resume_suggestions: list[str]
    # 每个维度的标准化得分和本轮实际参与计算的权重，便于前端解释。
    dimension_scores: dict[str, float] = field(default_factory=dict)
    applied_weights: dict[str, float] = field(default_factory=dict)


@dataclass
class ProjectExperienceCard:
    """项目证据分析产出的待确认项目经历卡片。

    这张卡片不能直接写入候选人档案；它只是把项目证据材料整理成
    技术栈、功能、职责草稿和待确认问题，等待候选人确认。

    ``source_*`` 字段记录证据来自本地目录还是公开 GitHub 仓库。它们只用于
    溯源和复核，不代表候选人已经确认自己负责过仓库中的全部代码。
    """

    card_type: str
    project_name: str
    read_files: list[str]
    skipped_summary: dict[str, int]
    detected_tech_stack: list[str]
    detected_core_features: list[str]
    responsibility_draft: list[str]
    highlight_draft: list[str]
    resume_expression_draft: list[str]
    questions_for_candidate: list[str]
    source_type: str = "local_directory"
    source_url: str | None = None
    source_ref: str | None = None
    discovered_file_kinds: dict[str, int] = field(default_factory=dict)
    deferred_files: list[str] = field(default_factory=list)


@dataclass
class ProjectExperienceRecord:
    """已经保存到结构化事实源的项目经历卡片记录。

    `card` 是系统分析出的待确认内容；`status` 和 `confirmed_summary`
    记录候选人是否确认过。确认项目卡片不会反向覆盖候选人档案里的结构化事实。
    """

    id: int
    candidate_id: int
    status: str
    card: ProjectExperienceCard
    confirmed_summary: str | None
    created_at: str
    confirmed_at: str | None


@dataclass
class ProjectArchiveImportRecord:
    """一次整包项目导入及其原件、任务状态和最终项目卡片关联。"""

    id: int
    account_id: int
    candidate_id: int
    knowledge_asset_id: int
    knowledge_asset_version_id: int
    project_card_id: int | None
    source_type: str
    source_url: str | None
    source_ref: str | None
    original_filename: str
    content_fingerprint: str
    status: str
    error_summary: str | None
    created_at: str
    updated_at: str


@dataclass
class ProjectArchiveFileRecord:
    """项目压缩包中的一个受控文件清单项，不包含文件正文。"""

    id: int
    project_archive_id: int
    relative_path: str
    file_kind: str
    media_type: str
    file_size: int
    compressed_size: int
    sha256: str | None
    analysis_status: str
    skip_reason: str | None
    metadata: dict[str, object]
    long_text_id: int | None = None
    extraction_method: str | None = None
    text_length: int = 0


@dataclass
class ProjectCollectionSessionRecord:
    """一次浏览器本地目录预扫描、采集和分析会话。"""

    id: int
    account_id: int
    candidate_id: int
    project_card_id: int | None
    project_name: str
    source_type: str
    manifest_fingerprint: str
    status: str
    file_count: int
    selected_file_count: int
    uploaded_file_count: int
    total_size: int
    selected_size: int
    error_summary: str | None
    created_at: str
    updated_at: str


@dataclass
class ProjectCollectionFileRecord:
    """本地目录清单中的文件及后端采集计划和提取状态。"""

    id: int
    collection_id: int
    relative_path: str
    file_kind: str
    media_type: str
    file_size: int
    client_sha256: str | None
    server_sha256: str | None
    selection_status: str
    selection_reason: str
    extraction_method: str | None
    text_length: int
    long_text_id: int | None
    metadata: dict[str, object]
    created_at: str
    updated_at: str


@dataclass
class ResumeDraft:
    """职位定制简历草稿正文。

    草稿是给候选人编辑和确认的表达结果，不是候选人档案事实源。
    `evidence_items` 说明正文来自哪些已确认材料，`authenticity_risks`
    记录缺口、LLM 回退或可能需要人工确认的风险。
    """

    title: str
    content: str
    evidence_items: list[str]
    authenticity_risks: list[str]
    rewrite_notes: list[str]
    llm_used: bool
    llm_discarded: bool


@dataclass
class ResumeDraftRecord:
    """已经保存的职位定制简历草稿版本。

    同一候选人针对同一职位可以生成多个版本；这些版本不会反向覆盖
    候选人档案，只作为可编辑草稿保存。
    """

    id: int
    candidate_id: int
    job_id: int
    version: int
    status: str
    draft: ResumeDraft
    created_at: str


@dataclass
class KnowledgeAssetRecord:
    """一份可跨解析器、检索器和业务功能复用的知识文件资产。"""

    id: int
    account_id: int
    candidate_id: int | None
    asset_kind: str
    title: str
    lifecycle_status: str
    current_version_id: int | None
    metadata: dict[str, object]
    created_at: str
    updated_at: str


@dataclass
class VisualKnowledgeItemRecord:
    """一张项目图片或 PDF 视觉页的可追溯知识资产。"""

    id: int
    account_id: int
    candidate_id: int
    project_archive_file_id: int | None
    project_collection_file_id: int | None
    long_text_id: int | None
    source_id: str
    source_label: str
    page_number: int | None
    media_type: str
    storage_key: str
    file_size: int
    sha256: str
    width: int
    height: int
    index_status: str
    embedding_model: str | None
    embedding_dimensions: int | None
    index_error_type: str | None
    metadata: dict[str, object]
    created_at: str
    updated_at: str


@dataclass
class KnowledgeAssetVersionRecord:
    """知识文件资产的一份不可变原件版本及其处理状态。"""

    id: int
    asset_id: int
    version_number: int
    is_current: bool
    original_filename: str
    storage_key: str
    media_type: str
    file_size: int
    sha256: str
    source_kind: str
    source_url: str | None
    revision_label: str | None
    processing_status: str
    scan_status: str
    scan_engine: str | None
    scan_reason: str | None
    metadata: dict[str, object]
    created_at: str


@dataclass
class ResumeArtifactRecord:
    """一份已上传或已生成的简历文件版本。

    结构化事实源只保存文件元数据、提取文本和归属关系；二进制文件位于受控文件目录。
    `parent_artifact_id` 把职位定制文件关联回原始上传文件，避免覆盖源文件。
    """

    id: int
    account_id: int | None
    candidate_id: int
    job_id: int | None
    draft_id: int | None
    parent_artifact_id: int | None
    version: int
    artifact_type: str
    original_filename: str
    download_filename: str
    storage_key: str
    media_type: str
    file_size: int
    sha256: str
    extraction_method: str
    text_length: int
    page_count: int | None
    status: str
    long_text_id: int | None
    created_at: str
    knowledge_asset_id: int | None = None
    knowledge_asset_version_id: int | None = None
    scan_status: str = "clean"
    scan_engine: str | None = None
    scan_reason: str | None = None


@dataclass
class TailoredResumeResult:
    """一次基于上传简历生成职位定制文件的结果。"""

    draft: ResumeDraftRecord
    artifacts: list[ResumeArtifactRecord]


@dataclass
class LongTextRecord:
    """结构化数据库 `long_texts` 表中的一条长文本材料。

    它是 RAG 索引的输入来源，但仍然不是向量库本身。RAG 层必须保留这些来源字段，
    方便候选人追溯“这段证据来自哪里”。
    """

    id: int
    entity_type: str
    entity_id: int
    source_label: str
    text: str
    # 账号级 metadata 用于向量检索隔离；旧离线记录可以为空。
    account_id: int | None = None
    # 候选人归属用于 pgvector 表的外键和后续候选人级检索；职位公共材料可以为空。
    candidate_id: int | None = None


@dataclass
class ChatMessageRecord:
    """网页聊天窗口中的一条持久化消息。

    这张记录只用于恢复用户界面中的对话历史，不作为候选人档案事实源。
    真正会影响匹配、简历改写的事实仍然必须经过结构化档案或长文本/RAG 流程。
    """

    id: int
    candidate_id: int
    session_id: str
    role: str
    content: str
    metadata: dict[str, object]
    created_at: str


@dataclass
class RAGIndexStats:
    """一次 RAG 索引写入的统计结果。

    `mode` 用来区分全量重建和增量替换；两种模式都只描述向量索引动作，
    不改变结构化事实源。
    """

    document_count: int
    chunk_count: int
    persist_directory: str
    collection_name: str
    mode: str = "rebuild"


@dataclass
class RAGSearchResult:
    """RAG 检索返回的证据片段。

    `distance` 来自向量库，数值越小通常表示越相关；它不是事实可信度。
    事实可信度仍然要回到结构化事实和候选人确认状态判断。
    """

    content: str
    entity_type: str
    entity_id: int
    source_label: str
    long_text_id: int
    chunk_index: int
    distance: float
    evidence_kind: str = "text"
    visual_item_id: int | None = None
    page_number: int | None = None


@dataclass
class CandidateProfilePatch:
    """对候选人档案的局部更新。

    对话式自动入库不会要求用户一次性提交完整档案；LLM/规则只提取当前消息中
    明确出现的字段，然后通过这个 patch 合并到结构化档案。
    """

    status: str | None = None
    education: str | None = None
    experience_years: float | None = None
    skills: dict[str, str] = field(default_factory=dict)
    preferred_cities: list[str] = field(default_factory=list)
    salary_floor_k: int | None = None
    expected_salary_k: int | None = None
    target_directions: list[str] = field(default_factory=list)
    # 用户明确说“方向改为 X”时，用 X 替换旧方向；普通补充仍按列表追加。
    replace_target_directions: bool = False
    unacceptable: list[str] = field(default_factory=list)
    # 对话中明确表示“也可以”的城市，不会覆盖首选城市。
    acceptable_cities: list[str] = field(default_factory=list)
    # 空列表本身表示“没有提取到城市”；这些标记用于表达明确清除意图。
    replace_preferred_cities: bool = False
    clear_preferred_cities: bool = False
    clear_acceptable_cities: bool = False
    # 只保存本轮明确表达的维度，未出现的维度不覆盖档案原值。
    preference_weights: dict[str, float] = field(default_factory=dict)


@dataclass
class LongTextInput:
    """一次自动入库希望保存的长文本材料。"""

    entity_type: str
    entity_id: int
    source_label: str
    text: str


@dataclass
class ConversationIngestionDecision:
    """对一条对话资料的保存决策。

    `profile_updates` 表示进入结构化档案的内容；`long_texts` 表示进入
    长文本材料库、后续可同步到 RAG 的内容；`reply` 是 agent 对用户的回复。
    """

    reply: str
    profile_updates: CandidateProfilePatch
    long_texts: list[LongTextInput]


@dataclass
class ConversationIngestionResult:
    """对话式自动入库执行后的结果摘要。"""

    candidate_id: int
    reply: str
    saved_structured_fields: list[str]
    saved_long_text_ids: list[int]
    rag_rebuilt: bool
    rag_index_stats: RAGIndexStats | None = None
    rag_update_mode: str = "none"


@dataclass
class AgentChatResult:
    """LangChain Agent 一轮对话的执行摘要。

    这个对象面向 Web/API 等用户入口：除了最终回复，还会记录本轮实际使用了哪些
    工具，以及工具输出的结构化摘要，方便界面展示和后续调试。
    """

    reply: str
    candidate_id: int | None
    session_id: str
    mode: str
    used_tools: list[str] = field(default_factory=list)
    tool_outputs: list[dict[str, object]] = field(default_factory=list)
    # 当前轮可从供应商响应中确认的用量摘要；缺失时使用 None/0，不把估算值冒充精确账单。
    usage: dict[str, int | str] = field(default_factory=dict)
    root_request_id: str = ""
    # 只保存路由决策和耗时等低敏观测，不保存用户消息、提示词或模型原始回复。
    routing: dict[str, object] = field(default_factory=dict)


@dataclass
class AccountRecord:
    """账号记录。

    账号是共享访问和统一计费的主体。一个账号可以包含多个求职者档案，
    因此这里不把账号和某一个候选人绑定。密码只保存哈希，绝不会出现在这个模型中。
    """

    id: int
    email: str
    display_name: str | None
    role: str
    status: str
    created_at: str
    updated_at: str
    must_change_password: bool = False
    email_verified_at: str | None = None
    deleted_at: str | None = None


@dataclass
class AccountEmailOutboxRecord:
    """一封账号事务邮件的持久投递状态。"""

    id: int
    account_id: int
    action_token_id: int
    purpose: str
    recipient_email: str
    delivery_key: str
    status: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: str
    claimed_at: str | None
    sent_at: str | None
    last_error_type: str | None
    last_error_summary: str | None
    created_at: str
    updated_at: str


@dataclass
class AccountBalanceSummary:
    """一个账号的余额与消费汇总。"""

    account_id: int
    balance_micro_yuan: int
    total_recharge_micro_yuan: int
    total_consumed_micro_yuan: int
    ledger_entry_count: int
    low_balance_threshold_micro_yuan: int
    state: str = "balance"
    state_label: str = "余额"


@dataclass
class BalanceLedgerRecord:
    """一条余额或消费流水。"""

    id: int
    account_id: int
    entry_kind: str
    amount_micro_yuan: int
    balance_before_micro_yuan: int
    balance_after_micro_yuan: int
    token_count: int | None
    price_per_million_tokens_yuan: float | None
    source_reference: str | None
    summary: str
    operator_account_id: int | None = None
    recharge_order_id: int | None = None
    details: dict[str, object] = field(default_factory=dict)
    created_at: str = ""


@dataclass
class RechargeOrderRecord:
    """一笔用户充值订单；管理员人工补款不会伪装成支付订单。"""

    id: int
    order_number: str
    account_id: int
    created_by_account_id: int | None
    amount_micro_yuan: int
    status: str
    payment_provider: str
    provider_order_id: str | None
    idempotency_key: str
    description: str
    failure_reason: str | None = None
    details: dict[str, object] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    paid_at: str | None = None
    cancelled_at: str | None = None
    refunded_at: str | None = None


@dataclass
class PaymentEventRecord:
    """支付渠道事件的低敏幂等记录。"""

    id: int
    recharge_order_id: int
    payment_provider: str
    provider_event_id: str
    event_type: str
    processing_status: str
    signature_valid: bool
    payload_sha256: str
    error_summary: str | None = None
    details: dict[str, object] = field(default_factory=dict)
    received_at: str = ""
    processed_at: str | None = None


@dataclass
class AuthSessionRecord:
    """服务端登录 Session 记录。

    `token_hash` 是浏览器 Cookie 中随机令牌的 SHA-256 摘要，原始令牌只在登录响应
    中短暂返回，数据库不会保存可直接使用的登录凭证。
    """

    id: int
    account_id: int
    token_hash: str
    created_at: str
    last_seen_at: str
    expires_at: str
    absolute_expires_at: str
    revoked_at: str | None
    user_agent: str | None
    ip_address: str | None


@dataclass
class ChatSessionRecord:
    """一个求职者档案下的独立持久化对话。"""

    id: int
    session_id: str
    account_id: int
    candidate_id: int
    job_id: int | None
    title: str
    status: str
    created_at: str
    updated_at: str
    archived_at: str | None


@dataclass
class UsageEventRecord:
    """一次真实上游调用的追加式 Token 用量流水。

    `usage_source` 用来区分供应商确认、估算、缺失和本地零成本操作；正式计费时
    只采用供应商确认的用量。`root_request_id` 可以把一轮用户操作中的多个模型调用
    关联起来，`call_id` 则用于幂等和重试排查。
    """

    id: int
    account_id: int
    candidate_id: int | None
    session_id: str | None
    root_request_id: str | None
    call_id: str
    provider: str
    model: str
    operation: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    usage_source: str
    status: str
    attempt: int
    provider_request_id: str | None
    raw_usage: dict[str, object]
    created_at: str
    billable: bool
    pricing_version: str | None


@dataclass
class ToolCallTraceRecord:
    """一次用户任务的工具调用审计轨迹。"""

    id: int
    account_id: int
    candidate_id: int | None
    session_id: str | None
    root_request_id: str
    title: str
    status: str
    source: str
    step_count: int
    attempt_count: int
    last_step_name: str | None
    last_error_summary: str | None
    trace: dict[str, object] = field(default_factory=dict)
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str = ""


@dataclass
class AdminAuditEventRecord:
    """一次管理员动作的追加式审计记录。"""

    id: int
    actor_account_id: int | None
    target_account_id: int | None
    action: str
    target_type: str
    target_id: str | None
    outcome: str
    summary: str
    details: dict[str, object] = field(default_factory=dict)
    request_id: str | None = None
    created_at: str = ""


@dataclass
class BackgroundTaskRecord:
    """一条可跨 Web 重启恢复的后台任务状态。

    `task_key` 同时作为 Celery 消息 ID 和对外查询标识；`payload` 仅保存资源 ID
    等受控引用，不能保存简历正文、密码或模型提示词。Web API 不直接回显 payload。
    """

    id: int
    task_key: str
    account_id: int
    candidate_id: int | None
    session_id: str | None
    task_type: str
    status: str
    progress: int
    attempt: int
    max_attempts: int
    idempotency_key: str | None
    payload: dict[str, object]
    result: dict[str, object]
    error_summary: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str
