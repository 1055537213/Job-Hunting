"""标准 LangChain Agent 门面。

这个模块把用户可见的聊天入口统一收束为一条标准链路：

Web/API
    -> JobHuntingAgent
    -> LangChain create_agent
    -> Tools
    -> JobHuntingApp
    -> PostgreSQL + pgvector / LLM

其中：

- PostgreSQL 是结构化事实源和长文本事实源；pgvector 是唯一的语义索引。
- long_texts 仍然是长文本材料登记处。
- RAG 仍然只是派生语义索引，不单独充当事实源。
- Agent 不直接改数据库，只能通过工具调用 `JobHuntingApp`。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langgraph.checkpoint.memory import MemorySaver

from .app import JobHuntingApp
from .config import DEFAULT_ENV_PATH, AgentMemorySettings, load_agent_memory_settings
from .conversation_memory import build_restored_context_messages
from .deduplication import DuplicateResourceError
from .job_parser import InvalidJobTextError
from .llm import extract_message_text
from .models import AgentChatResult, BackgroundTaskRecord


class JobHuntingAgentContext(TypedDict):
    """LangChain Agent 运行时上下文。

    `candidate_id` 让工具知道当前服务的是哪一个候选人。
    `use_tool_llm` 控制工具内部是否继续调用真实大模型。
    `default_auto_rag` 让前端勾选项能传到工具层，而不是写死在 prompt 里。
    """

    candidate_id: int | None
    account_id: int | None
    session_id: str
    root_request_id: str
    use_tool_llm: bool
    default_auto_rag: bool


AGENT_SYSTEM_PROMPT = """
你是一个本地运行的求职助手 LangChain Agent。

你的职责：
1. 在当前会话已经绑定候选人档案时，帮用户整理并保存候选人资料。
2. 基于本地已导入职位做匹配分析。
3. 为职位生成职位定制简历草稿，或基于用户已上传的简历生成 DOCX/PDF 文件。
4. 对候选人主动提供的公开 GitHub 项目进行分析，并等待候选人确认项目摘要。

你必须遵守这些边界：
- 结构化事实只能通过工具写入结构化事实源。
- 长文本材料只能通过工具写入 long_texts，再由工具决定是否增量进入 RAG。
- RAG 检索只是证据索引，不是事实源。
- 不能登录 BOSS、不能爬取网站、不能自动投递、不能自动发送 HR 消息。
- 不能要求或读取用户电脑上的本地路径；项目分析只接受公开 GitHub 仓库首页链接。
- 不要假装已经执行某个保存/导入/匹配动作；只有在工具返回结果后才能确认。
- 如果当前会话还没有绑定候选人，就先用 list_candidate_profiles 帮用户确认当前有哪些档案，
  并明确提醒用户先创建或选择候选人，再继续保存资料。
- 当用户补充资料时，优先调用 ingest_candidate_message。
- 当用户提供公开 GitHub 仓库首页链接并要求分析项目时，调用 analyze_github_project_for_candidate；
  任务排队后要明确告诉用户等待 Worker 完成，不要声称已经读完仓库。
- 当用户问“适合哪些岗位”时，优先调用 match_all_jobs_for_candidate。
- 当用户让你改简历时，先调用 list_resume_artifacts_for_candidate 查看是否有原始上传文件。
- 如果存在原始上传文件，优先调用 create_tailored_resume_from_upload，并把返回的下载链接告诉用户。
- 如果没有上传文件，才调用 create_resume_draft_for_job 生成纯文本草稿。
- 用户要求提高熟练度措辞时，必须先提示真实性风险并等待再次确认；只有确认后的
  后续工具调用才能把 `allow_proficiency_upgrade` 设为 true。

最终回复请使用中文，先说你已经完成了什么，再简洁说明下一步建议。
""".strip()


CONVERSATION_SUMMARY_PROMPT = """
你是求职助手 Agent 的对话记忆压缩器。

请把下面即将被替换的历史消息压缩成后续对话仍然需要的上下文。必须遵守：
1. 保留用户明确说过的候选人事实、偏好、求职方向、职位选择、HR 对话要点和待办。
2. 保留本轮或历史中已经执行过的重要动作，例如保存资料、导入职位、生成草稿、匹配职位。
3. 不要编造学历、年限、技能熟练度、项目成果数字或投递状态。
4. 如果某项内容只是推测或待确认，请标记为“待确认”。
5. 用中文输出，尽量短，但不要丢失会影响后续判断的边界。

历史消息：
{messages}
""".strip()


class JobHuntingAgent:
    """求职助手的标准 LangChain Agent 门面。"""

    def __init__(
        self,
        app: JobHuntingApp,
        env_path: str | Path = DEFAULT_ENV_PATH,
        model: BaseChatModel | None = None,
        memory_settings: AgentMemorySettings | None = None,
    ):
        """创建一个绑定本地应用服务的 LangChain Agent。

        传入 `model` 时常用于测试：这样可以注入假模型，而不用真的访问网络。
        生产环境则从 `.env` 自动构造 DeepSeek / OpenAI-compatible ChatModel。
        """

        self.app = app
        self.model_gateway = app.model_gateway
        self.env_path = Path(env_path)
        if model is None:
            # Agent 不直接创建供应商 SDK；模型选择、重试策略和后续 usage 统一由
            # 内部 Model Gateway 收束。
            self.model = self.model_gateway.chat_model("agent_chat")
            self.tool_llm_available = True
        else:
            self.model = model
            # 注入主模型常用于离线测试或自托管模型。工具内的二次单轮 LLM 仍由
            # `.env` 独立创建；配置不存在时使用规则实现，而不是让整轮 Agent 返回 502。
            try:
                _ = self.model_gateway.llm_settings
            except ValueError:
                self.tool_llm_available = False
            else:
                self.tool_llm_available = True
        self.memory_settings = memory_settings or load_agent_memory_settings(self.env_path)
        self._restored_sessions: set[tuple[int | None, int | None, str]] = set()
        self.graph = create_agent(
            model=self.model,
            tools=build_job_hunting_tools(app),
            system_prompt=AGENT_SYSTEM_PROMPT,
            middleware=build_memory_middleware(self.model, self.memory_settings),
            context_schema=JobHuntingAgentContext,
            checkpointer=MemorySaver(),
            name="job_hunting_agent",
        )

    def chat(
        self,
        message: str,
        candidate_id: int | None,
        session_id: str | None = None,
        use_tool_llm: bool = True,
        auto_rag: bool = True,
        account_id: int | None = None,
        root_request_id: str | None = None,
    ) -> AgentChatResult:
        """执行一轮标准 LangChain Agent 对话。"""

        resolved_session_id = session_id or default_session_id(candidate_id, account_id)
        root_request_id = root_request_id or uuid.uuid4().hex
        result = self.graph.invoke(
            {"messages": self.build_turn_messages(message, candidate_id, resolved_session_id, account_id)},
            config={"configurable": {"thread_id": scoped_thread_id(account_id, candidate_id, resolved_session_id)}},
            context={
                "candidate_id": candidate_id,
                "account_id": account_id,
                "session_id": resolved_session_id,
                "use_tool_llm": use_tool_llm and self.tool_llm_available,
                "default_auto_rag": auto_rag,
                "root_request_id": root_request_id,
            },
        )
        messages = list(result.get("messages", []))
        usage = self.model_gateway.record_chat_messages(
            operation="agent_chat",
            messages=messages,
            account_id=account_id,
            candidate_id=candidate_id,
            session_id=resolved_session_id,
            root_request_id=root_request_id,
        )
        return AgentChatResult(
            reply=extract_final_reply(messages),
            candidate_id=candidate_id,
            session_id=resolved_session_id,
            mode="langchain_agent",
            used_tools=collect_used_tools(messages),
            tool_outputs=collect_tool_outputs(messages),
            usage=usage,
            root_request_id=root_request_id,
        )

    def stream_chat(
        self,
        message: str,
        candidate_id: int | None,
        session_id: str | None = None,
        use_tool_llm: bool = True,
        auto_rag: bool = True,
        account_id: int | None = None,
        root_request_id: str | None = None,
    ) -> Iterator[dict[str, object]]:
        """流式执行一轮标准 LangChain Agent 对话。

        事件格式面向 Web SSE：

        - `{"type": "token", "content": "..."}`：模型增量文本。
        - `{"type": "step_started", "name": "..."}`：工具步骤开始。
        - `{"type": "step_completed", "name": "..."}`：工具步骤完成及摘要。
        - `{"type": "final", "result": AgentChatResult}`：完整结果，供落库和刷新 UI。

        注意：工具调用可能在模型回复中间发生，所以最终仍要发送 `final` 事件，
        让前端拿到工具摘要、候选人档案更新和可持久化的完整回复。
        """

        resolved_session_id = session_id or default_session_id(candidate_id, account_id)
        root_request_id = root_request_id or uuid.uuid4().hex
        tool_and_final_messages: list[BaseMessage] = []
        streamed_reply_parts: list[str] = []
        streamed_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        active_tool_names: set[str] = set()

        for stream_item in self.graph.stream(
            {"messages": self.build_turn_messages(message, candidate_id, resolved_session_id, account_id)},
            config={"configurable": {"thread_id": scoped_thread_id(account_id, candidate_id, resolved_session_id)}},
            context={
                "candidate_id": candidate_id,
                "account_id": account_id,
                "session_id": resolved_session_id,
                "use_tool_llm": use_tool_llm and self.tool_llm_available,
                "default_auto_rag": auto_rag,
                "root_request_id": root_request_id,
            },
            stream_mode="messages",
        ):
            streamed_message = unpack_stream_message(stream_item)
            if streamed_message is None:
                continue

            if isinstance(streamed_message, ToolMessage):
                tool_and_final_messages.append(streamed_message)
                tool_name = streamed_message.name or "unknown_tool"
                # 某些模型不会在 AIMessageChunk 中携带完整 tool call 名称；
                # 收到 ToolMessage 时补发开始事件，保证前端不会只看到“完成”。
                if tool_name not in active_tool_names:
                    active_tool_names.add(tool_name)
                    yield {"type": "step_started", "name": tool_name}
                tool_output = tool_message_to_output(streamed_message)
                active_tool_names.discard(tool_name)
                yield {
                    "type": "step_completed",
                    "name": tool_name,
                    "data": tool_output.get("data"),
                }
                continue

            if isinstance(streamed_message, AIMessageChunk):
                merge_usage(streamed_usage, extract_usage_metadata(streamed_message))
                for tool_name in extract_tool_call_names(streamed_message):
                    if tool_name not in active_tool_names:
                        active_tool_names.add(tool_name)
                        yield {"type": "step_started", "name": tool_name}
                token = extract_stream_token(streamed_message)
                if token:
                    streamed_reply_parts.append(token)
                    yield {"type": "token", "content": token}
                continue

            if isinstance(streamed_message, AIMessage):
                # FakeChatModel 等测试模型可能一次性给出完整 AIMessage，而不是 AIMessageChunk。
                tool_and_final_messages.append(streamed_message)
                for tool_name in extract_tool_call_names(streamed_message):
                    if tool_name not in active_tool_names:
                        active_tool_names.add(tool_name)
                        yield {"type": "step_started", "name": tool_name}
                token = extract_stream_token(streamed_message)
                if token and not streamed_message.tool_calls:
                    streamed_reply_parts.append(token)
                    yield {"type": "token", "content": token}

        reply = extract_final_reply(tool_and_final_messages)
        if reply.startswith("本轮没有生成") and streamed_reply_parts:
            reply = "".join(streamed_reply_parts).strip()
        usage = self.model_gateway.record_chat_usage_summary(
            self.model_gateway.new_call_context(
                "agent_chat",
                account_id=account_id,
                candidate_id=candidate_id,
                session_id=resolved_session_id,
                root_request_id=root_request_id,
                call_id=f"{root_request_id}-agent_chat-stream",
            ),
            streamed_usage,
        )
        yield {
            "type": "final",
            "result": AgentChatResult(
                reply=reply,
                candidate_id=candidate_id,
                session_id=resolved_session_id,
                mode="langchain_agent",
                used_tools=collect_used_tools(tool_and_final_messages),
                tool_outputs=collect_tool_outputs(tool_and_final_messages),
                usage=usage,
                root_request_id=root_request_id,
            ),
        }

    def build_turn_messages(
        self,
        message: str,
        candidate_id: int | None,
        session_id: str,
        account_id: int | None = None,
    ) -> list[BaseMessage]:
        """构造本轮输入消息，并在新进程首次会话时恢复持久化聊天历史。"""

        restored_messages = self.restore_persistent_history(candidate_id, session_id, account_id)
        return [*restored_messages, HumanMessage(content=message)]

    def restore_persistent_history(
        self,
        candidate_id: int | None,
        session_id: str,
        account_id: int | None = None,
    ) -> list[BaseMessage]:
        """从持久化 `chat_messages` 恢复一次历史上下文。

        `MemorySaver` 只在当前进程里有效；服务重启后它是空的。这里在每个
        `(candidate_id, session_id)` 首次调用时读取数据库历史，把页面上能恢复的
        对话也恢复到模型上下文里。之后同一进程内交给 LangGraph checkpointer 累积。
        """

        if not self.memory_settings.enabled or candidate_id is None:
            return []
        session_key = (account_id, candidate_id, session_id)
        if session_key in self._restored_sessions:
            return []
        self._restored_sessions.add(session_key)
        records = self.app.list_chat_messages(
            candidate_id,
            session_id,
            limit=self.memory_settings.restore_history_limit,
            account_id=account_id,
        )
        return build_restored_context_messages(records, self.memory_settings)


def build_memory_middleware(
    model: BaseChatModel,
    settings: AgentMemorySettings,
) -> list[object]:
    """构建 LangChain 运行时上下文压缩中间件。"""

    if not settings.enabled:
        return []
    return [
        SummarizationMiddleware(
            model=model,
            trigger=("tokens", settings.summary_trigger_tokens),
            keep=("messages", settings.summary_keep_messages),
            summary_prompt=CONVERSATION_SUMMARY_PROMPT,
            trim_tokens_to_summarize=settings.summary_trim_tokens,
        )
    ]


def build_job_hunting_tools(app: JobHuntingApp) -> list[object]:
    """构建标准 LangChain Agent 工具列表。"""

    @tool
    def ingest_candidate_message(
        message: str,
        runtime: ToolRuntime[JobHuntingAgentContext, Any],
        auto_rag: bool | None = None,
    ) -> str:
        """当用户补充候选人资料、技能、项目经历或 HR 对话时，自动保存到 PostgreSQL 和 RAG。"""

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        account_id = context.get("account_id")
        llm_client = (
            app.model_gateway.llm_client(
                app.model_gateway.new_call_context(
                    "tool_llm_ingestion",
                    account_id=account_id,
                    candidate_id=candidate_id,
                    session_id=context.get("session_id"),
                    root_request_id=context.get("root_request_id"),
                )
            )
            if context["use_tool_llm"]
            else None
        )
        result = app.ingest_conversation_message(
            candidate_id,
            message,
            llm_client=llm_client,
            auto_rebuild_rag=context["default_auto_rag"] if auto_rag is None else auto_rag,
            account_id=account_id,
        )
        return dumps_tool_output(
            {
                "candidate_id": candidate_id,
                "reply": result.reply,
                "saved_structured_fields": result.saved_structured_fields,
                "saved_long_text_ids": result.saved_long_text_ids,
                "rag_update_mode": result.rag_update_mode,
            }
        )

    @tool
    def get_current_candidate_profile(runtime: ToolRuntime[JobHuntingAgentContext, Any]) -> str:
        """读取当前候选人的结构化档案，用于回答“我现在的档案里有什么”。"""

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        return dumps_tool_output(asdict(app.get_candidate_profile(candidate_id, account_id=context.get("account_id"))))

    @tool
    def list_candidate_profiles(runtime: ToolRuntime[JobHuntingAgentContext, Any]) -> str:
        """列出本地所有候选人档案，适合在用户不确定当前档案时使用。"""

        # 同账号内可以共享所有档案，但不能看到其它账号的档案。
        context = require_runtime_context(runtime)
        return dumps_tool_output(
            {"profiles": [asdict(profile) for profile in app.list_candidate_profiles(account_id=context.get("account_id"))]}
        )

    @tool
    def search_candidate_evidence(
        query: str,
        runtime: ToolRuntime[JobHuntingAgentContext, Any],
        top_k: int = 5,
        entity_types: list[str] | None = None,
    ) -> str:
        """从本地 RAG 索引检索候选人证据片段，但不要把结果直接当成新的事实。"""

        context = require_runtime_context(runtime)
        results = app.search_rag(
            query,
            top_k,
            entity_types,
            account_id=context.get("account_id"),
        )
        return dumps_tool_output({"query": query, "results": [asdict(item) for item in results]})

    @tool
    def import_job_from_text(
        raw_text: str,
        runtime: ToolRuntime[JobHuntingAgentContext, Any],
        source_url: str | None = None,
    ) -> str:
        """导入用户主动复制回来的职位文本，并解析成标准化职位记录。"""

        try:
            # Agent 的 `use_tool_llm=False` 不仅用于资料入库和简历工具，也必须传给
            # 职位技能分类。否则测试或离线规则模式仍可能意外发起真实模型请求。
            job = app.import_job_text(
                raw_text,
                source_url,
                account_id=runtime.context.get("account_id"),
                classify_with_llm=runtime.context["use_tool_llm"],
            )
        except (InvalidJobTextError, DuplicateResourceError) as error:
            # 工具返回可读失败结果，避免 Agent 把非职位文本误认为已经成功入库。
            return dumps_tool_output({"saved": False, "error": str(error)})
        return dumps_tool_output({"job": asdict(job)})

    @tool
    def list_imported_jobs(runtime: ToolRuntime[JobHuntingAgentContext, Any]) -> str:
        """列出本地已经导入的职位池，供后续匹配或简历改写选择。"""

        context = require_runtime_context(runtime)
        return dumps_tool_output({"jobs": [asdict(job) for job in app.list_jobs(account_id=context.get("account_id"))]})

    @tool
    def match_all_jobs_for_candidate(runtime: ToolRuntime[JobHuntingAgentContext, Any]) -> str:
        """匹配当前候选人与本地全部职位，并返回按推荐顺序排序的结果。"""

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        account_id = context.get("account_id")
        jobs_by_id = {job.id: job for job in app.list_jobs(account_id=account_id)}
        matches = app.match_all_jobs(candidate_id, account_id=account_id)
        return dumps_tool_output(
            {
                "candidate_id": candidate_id,
                "matches": [
                    {
                        "job": asdict(jobs_by_id[match.job_id]),
                        "match": asdict(match),
                    }
                    for match in matches
                ],
            }
        )

    @tool
    def list_project_cards_for_candidate(runtime: ToolRuntime[JobHuntingAgentContext, Any]) -> str:
        """列出当前候选人的项目经历卡片，查看哪些还待确认。"""

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        cards = app.list_project_cards(candidate_id, account_id=context.get("account_id"))
        return dumps_tool_output({"project_cards": [asdict(card) for card in cards]})

    @tool
    def analyze_github_project_for_candidate(
        repository_url: str,
        runtime: ToolRuntime[JobHuntingAgentContext, Any],
    ) -> str:
        """分析公开 GitHub 仓库并保存成待确认项目经历卡片。

        只接受 ``https://github.com/owner/repository`` 形式的公开仓库首页链接。
        网页运行环境会把任务交给 Worker；工具返回排队状态，不会假装已经完成分析。
        """

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        account_id = context.get("account_id")
        try:
            if app.task_queue_enabled:
                if account_id is None:
                    raise ValueError("GitHub 项目分析任务缺少账号归属。")
                task = app.enqueue_github_project_analysis_task(
                    repository_url=repository_url,
                    account_id=account_id,
                    candidate_id=candidate_id,
                    session_id=context.get("session_id"),
                    root_request_id=context.get("root_request_id"),
                )
                return dumps_tool_output(
                    {
                        "task": background_task_tool_payload(task),
                        "message": "GitHub 项目分析任务已排队，完成后会生成待确认项目经历卡片。",
                    }
                )
            record = app.analyze_github_project_for_candidate(
                candidate_id,
                repository_url,
                account_id=account_id,
            )
        except DuplicateResourceError as error:
            return dumps_tool_output({"saved": False, "error": str(error)})
        return dumps_tool_output(asdict(record))

    @tool
    def confirm_project_card(
        record_id: int,
        runtime: ToolRuntime[JobHuntingAgentContext, Any],
        confirmed_summary: str | None = None,
    ) -> str:
        """确认一张项目卡片，并把候选人确认摘要保存为后续可检索证据。

        真实性边界：只有在候选人已经明确确认内容时，才应该调用这个工具。
        它会把“待确认卡片”提升为后续可引用的项目证据，但不会反向覆盖候选人档案。
        """

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        allowed_record_ids = {
            record.id
            for record in app.list_project_cards(candidate_id, account_id=context.get("account_id"))
        }
        if record_id not in allowed_record_ids:
            raise ValueError(f"项目卡片 {record_id} 不属于当前候选人 {candidate_id}。")
        account_id = context.get("account_id")
        if account_id is None:
            raise ValueError("确认项目经历缺少账号归属。")
        record, rag_task = app.confirm_project_card_and_enqueue_rag(
            record_id,
            confirmed_summary,
            account_id=account_id,
            session_id=context.get("session_id"),
            root_request_id=context.get("root_request_id"),
        )
        return dumps_tool_output(
            {
                "project_card": asdict(record),
                "task": background_task_tool_payload(rag_task) if rag_task is not None else None,
            }
        )

    @tool
    def create_resume_draft_for_job(
        job_id: int,
        runtime: ToolRuntime[JobHuntingAgentContext, Any],
        use_rag: bool = True,
        rag_query: str | None = None,
    ) -> str:
        """为当前候选人生成职位定制简历草稿，并保存成单独版本，不覆盖原档案。"""

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        account_id = context.get("account_id")
        llm_client = (
            app.model_gateway.llm_client(
                app.model_gateway.new_call_context(
                    "resume_rewrite",
                    account_id=account_id,
                    candidate_id=candidate_id,
                    session_id=context.get("session_id"),
                    root_request_id=context.get("root_request_id"),
                )
            )
            if context["use_tool_llm"]
            else None
        )
        draft = app.create_resume_draft(
            candidate_id,
            job_id,
            llm_client=llm_client,
            rag_query=rag_query,
            use_rag=use_rag,
            account_id=account_id,
        )
        return dumps_tool_output(asdict(draft))

    @tool
    def list_resume_artifacts_for_candidate(runtime: ToolRuntime[JobHuntingAgentContext, Any]) -> str:
        """列出当前候选人已上传和已生成的简历文件，供改写前选择源文件。"""

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        artifacts = app.list_resume_artifacts(
            candidate_id,
            account_id=context.get("account_id"),
        )
        return dumps_tool_output(
            {
                "artifacts": [
                    resume_artifact_tool_payload(artifact)
                    for artifact in artifacts
                ]
            }
        )

    @tool
    def create_tailored_resume_from_upload(
        source_artifact_id: int,
        job_id: int,
        runtime: ToolRuntime[JobHuntingAgentContext, Any],
        use_rag: bool = True,
        rag_query: str | None = None,
        allow_proficiency_upgrade: bool = False,
    ) -> str:
        """基于当前候选人的原始上传简历生成职位定制 DOCX/PDF 和独立草稿版本。

        默认不得拔高技能熟练度。只有已提示风险且用户再次确认提高措辞时，才能把
        `allow_proficiency_upgrade` 设为 true；生成结果始终不会覆盖原文件或档案。
        """

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        account_id = context.get("account_id")
        llm_client = (
            app.model_gateway.llm_client(
                app.model_gateway.new_call_context(
                    "resume_document_rewrite",
                    account_id=account_id,
                    candidate_id=candidate_id,
                    session_id=context.get("session_id"),
                    root_request_id=context.get("root_request_id"),
                )
            )
            if context["use_tool_llm"]
            else None
        )
        result = app.create_tailored_resume_from_artifact(
            candidate_id=candidate_id,
            source_artifact_id=source_artifact_id,
            job_id=job_id,
            llm_client=llm_client,
            rag_query=rag_query,
            use_rag=use_rag,
            allow_proficiency_upgrade=allow_proficiency_upgrade,
            account_id=account_id,
        )
        return dumps_tool_output(
            {
                "draft": asdict(result.draft),
                "artifacts": [
                    resume_artifact_tool_payload(artifact)
                    for artifact in result.artifacts
                ],
            }
        )

    return [
        ingest_candidate_message,
        get_current_candidate_profile,
        list_candidate_profiles,
        search_candidate_evidence,
        import_job_from_text,
        list_imported_jobs,
        match_all_jobs_for_candidate,
        list_project_cards_for_candidate,
        analyze_github_project_for_candidate,
        confirm_project_card,
        create_resume_draft_for_job,
        list_resume_artifacts_for_candidate,
        create_tailored_resume_from_upload,
    ]


def require_runtime_context(
    runtime: ToolRuntime[JobHuntingAgentContext, Any],
) -> JobHuntingAgentContext:
    """读取工具运行时上下文。"""

    if runtime is None:
        raise ValueError("当前工具缺少 Agent 运行时上下文。")
    return runtime.context


def require_candidate_id(context: JobHuntingAgentContext) -> int:
    """确保当前会话已经绑定候选人。"""

    candidate_id = context.get("candidate_id")
    if candidate_id is None:
        raise ValueError("当前会话还没有绑定候选人档案，请先创建或选择候选人。")
    return candidate_id


def build_tool_usage_callback(
    app: JobHuntingApp,
    context: JobHuntingAgentContext,
    operation: str,
):
    """为兼容旧调用方构造 Gateway 驱动的工具用量回调。"""

    call_context = app.model_gateway.new_call_context(
        operation,
        account_id=context.get("account_id"),
        candidate_id=context.get("candidate_id"),
        session_id=context.get("session_id"),
        root_request_id=context.get("root_request_id"),
    )

    def callback(message: object) -> None:
        """把一次单轮工具调用委托给 Gateway 记录。"""

        app.model_gateway.record_chat_response(call_context, message)

    return callback


def dumps_tool_output(value: dict[str, Any]) -> str:
    """统一序列化工具输出，方便 Agent 阅读，也方便 Web API 再解析。

    数据库驱动在读取 ``jobs.captured_at`` 等时间列时可能返回 ``datetime``，
    而 dataclass 转字典不会自动把它变成 JSON 标量。工具输出不应因某个可展示的
    数据库标量而中断 Agent 循环，因此把这类未知标量降级为其字符串表示。
    """

    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def resume_artifact_tool_payload(artifact) -> dict[str, Any]:
    """把简历文件记录转换为 Agent 可读且不泄露服务器路径的工具结果。"""

    payload = asdict(artifact)
    payload.pop("storage_key", None)
    payload.pop("account_id", None)
    payload["download_url"] = f"/api/resumes/{artifact.id}/download"
    return payload


def background_task_tool_payload(task: BackgroundTaskRecord) -> dict[str, Any]:
    """把后台任务压缩成 Agent 可读摘要，不暴露账号和任务 payload。"""

    return {
        "task_key": task.task_key,
        "task_type": task.task_type,
        "status": task.status,
        "progress": task.progress,
        "attempt": task.attempt,
        "max_attempts": task.max_attempts,
        "result": task.result,
        "error_summary": task.error_summary,
    }


def extract_usage_metadata(message: object) -> dict[str, int]:
    """从 LangChain 消息兼容读取供应商 token usage。"""

    usage = getattr(message, "usage_metadata", None)
    if not isinstance(usage, dict):
        response_metadata = getattr(message, "response_metadata", None)
        usage = response_metadata.get("token_usage") if isinstance(response_metadata, dict) else None
    if not isinstance(usage, dict):
        return {}
    input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)
    return {
        "input_tokens": max(0, input_tokens),
        "output_tokens": max(0, output_tokens),
        "total_tokens": max(0, total_tokens),
    }


def merge_usage(target: dict[str, int], incoming: dict[str, int]) -> None:
    """合并流式 usage；流式结束块通常携带累计值，因此取最大值避免重复累加。"""

    for key in ("input_tokens", "output_tokens", "total_tokens"):
        target[key] = max(target.get(key, 0), incoming.get(key, 0))


def record_usage_summary(
    app: JobHuntingApp,
    usage: dict[str, int],
    *,
    account_id: int | None,
    candidate_id: int | None,
    session_id: str,
    root_request_id: str,
    operation: str = "agent_model",
    call_id: str | None = None,
    model: str = "agent",
) -> dict[str, int | str]:
    """兼容旧接口，并把汇总 usage 委托给内部 Model Gateway。"""

    context = app.model_gateway.new_call_context(
        operation,
        account_id=account_id,
        candidate_id=candidate_id,
        session_id=session_id,
        root_request_id=root_request_id,
        call_id=call_id,
    )
    return app.model_gateway.record_usage(
        context,
        usage,
        provider="configured-llm",
        model=model,
    )


def record_agent_usage(
    app: JobHuntingApp,
    messages: list[BaseMessage],
    *,
    account_id: int | None,
    candidate_id: int | None,
    session_id: str,
    root_request_id: str,
) -> dict[str, int | str]:
    """兼容旧接口，并按每个 AIMessage 拆分为 Gateway 用量流水。"""

    return app.model_gateway.record_chat_messages(
        operation="agent_model",
        messages=messages,
        account_id=account_id,
        candidate_id=candidate_id,
        session_id=session_id,
        root_request_id=root_request_id,
    )


def default_session_id(candidate_id: int | None, account_id: int | None = None) -> str:
    """生成默认会话 ID。

    这里把候选人 ID 编进会话名，避免多个候选人共用同一段 Agent 历史记忆。
    """

    return f"account-{account_id or 'unknown'}-candidate-{candidate_id or 'unknown'}"


def scoped_thread_id(
    account_id: int | None,
    candidate_id: int | None,
    session_id: str,
) -> str:
    """为 LangGraph checkpointer 生成不可串线的线程键。

    Web 会话 ID 是可公开传输的业务标识，不能单独作为 MemorySaver 的键；
    把账号和候选人一起纳入后，即使同一账号下两个档案使用相同的自定义会话名，
    短期记忆也不会互相污染。
    """

    return f"account-{account_id or 'legacy'}:candidate-{candidate_id or 'unknown'}:session-{session_id}"


def extract_final_reply(messages: list[BaseMessage]) -> str:
    """从 Agent 消息列表中提取最终可展示回复。"""

    for message in reversed(messages):
        if isinstance(message, AIMessage):
            try:
                text = extract_message_text(message).strip()
            except Exception:  # noqa: BLE001 - 这里只是做最终展示兜底。
                text = str(message.content).strip()
            if text:
                return text
    return "本轮没有生成可展示回复，但相关工具可能已经执行。"


def unpack_stream_message(stream_item: object) -> BaseMessage | None:
    """从 LangGraph `stream_mode="messages"` 事件中取出消息对象。

    当前版本通常返回 `(message, metadata)`；这里做成宽松解析，方便后续
    LangChain/LangGraph 小版本调整时仍能兼容。
    """

    if isinstance(stream_item, BaseMessage):
        return stream_item
    if isinstance(stream_item, tuple) and stream_item:
        first_item = stream_item[0]
        if isinstance(first_item, BaseMessage):
            return first_item
    return None


def extract_stream_token(message: BaseMessage) -> str:
    """从流式消息中提取可增量展示的文本片段。"""

    try:
        return extract_message_text(message)
    except Exception:  # noqa: BLE001 - 流式展示失败时只丢弃该片段，不中断工具执行。
        content = getattr(message, "content", "")
        return content if isinstance(content, str) else ""


def extract_tool_call_names(message: BaseMessage) -> list[str]:
    """从完整或分片的 AI 消息中取出工具名称。"""

    names: list[str] = []
    for attribute in ("tool_calls", "tool_call_chunks"):
        calls = getattr(message, attribute, None) or []
        for call in calls:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if name and str(name) not in names:
                names.append(str(name))
    return names


def collect_used_tools(messages: list[BaseMessage]) -> list[str]:
    """按出现顺序收集本轮实际执行过的工具名。"""

    used_tools: list[str] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        tool_name = message.name or "unknown_tool"
        if tool_name not in used_tools:
            used_tools.append(tool_name)
    return used_tools


def collect_tool_outputs(messages: list[BaseMessage]) -> list[dict[str, object]]:
    """收集工具输出，尽量把 JSON 文本再解析成结构化结果。"""

    outputs: list[dict[str, object]] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        outputs.append(tool_message_to_output(message))
    return outputs


def tool_message_to_output(message: ToolMessage) -> dict[str, object]:
    """把一次工具消息压平成可供 Web 摘要使用的结构。"""

    raw_content = stringify_tool_message_content(message.content)
    item: dict[str, object] = {
        "tool_name": message.name or "unknown_tool",
        "raw_content": raw_content,
    }
    parsed = try_parse_json(raw_content)
    if parsed is not None:
        item["data"] = parsed
    return item


def stringify_tool_message_content(content: Any) -> str:
    """把 ToolMessage.content 统一压平成字符串。"""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item) for item in content)
    return str(content)


def try_parse_json(text: str) -> dict[str, Any] | list[Any] | None:
    """尝试把工具输出再解析成 JSON。"""

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, (dict, list)):
        return parsed
    return None
