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
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
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
from .intent_router import (
    DirectIntentExecutor,
    IntentDecision,
    IntentRouter,
    IntentRouterProtocol,
    IntentRoutingMetrics,
)
from .job_hunting_tools import build_job_hunting_tool_registry
from .langchain_tool_adapter import build_langchain_tools
from .llm import extract_message_text
from .models import AgentChatResult


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
- 网页对话始终绑定到用户当前选择的候选人档案；对话不能创建新的候选人档案，也不能把
  当前对话切换到其他档案。如果工具报告没有绑定档案，提醒用户先在工作台创建或选择档案。
- 当用户补充或修改当前档案资料时，调用 ingest_candidate_message。这个工具会受控地更新
  当前档案；工具成功后直接说明已更新的业务内容，不得声称系统没有修改接口。
- 当用户提供公开 GitHub 仓库首页链接并要求分析项目时，调用 analyze_github_project_for_candidate；
  任务排队后要明确告诉用户等待 Worker 完成，不要声称已经读完仓库。
- 当用户问“适合哪些岗位”时，优先调用 match_all_jobs_for_candidate。
- 当用户让你改简历时，先调用 list_resume_artifacts_for_candidate 查看是否有原始上传文件。
- 如果存在原始上传文件，优先调用 create_tailored_resume_from_upload，并把返回的下载链接告诉用户。
- 如果没有上传文件，才调用 create_resume_draft_for_job 生成纯文本草稿。
- 用户要求提高熟练度措辞时，必须先提示真实性风险并等待再次确认；只有确认后的
  后续工具调用才能把 `allow_proficiency_upgrade` 设为 true。

最终回复请使用中文，先说你已经完成了什么，再简洁说明下一步建议。不要向用户显示工具名、
内部字段名、数据库 ID、长文本 ID、RAG 更新模式或其他内部实现细节。
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
        intent_router: IntentRouterProtocol | None = None,
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
        self.intent_router = intent_router or IntentRouter(self.model_gateway)
        self.tool_registry = build_job_hunting_tool_registry(app)
        self.direct_intent_executor = DirectIntentExecutor(self.tool_registry)
        self.routing_metrics = IntentRoutingMetrics()
        self._restored_sessions: set[tuple[int | None, int | None, str]] = set()
        checkpointer = (
            MemorySaver()
            if self.memory_settings.checkpoint_backend == "memory"
            else None
        )
        self.graph = create_agent(
            model=self.model,
            tools=build_langchain_tools(self.tool_registry),
            system_prompt=AGENT_SYSTEM_PROMPT,
            middleware=build_memory_middleware(self.model, self.memory_settings),
            context_schema=JobHuntingAgentContext,
            checkpointer=checkpointer,
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

        turn_started_at = time.monotonic()
        resolved_session_id = session_id or default_session_id(candidate_id, account_id)
        root_request_id = root_request_id or uuid.uuid4().hex
        if account_id is not None:
            self.app.store.assert_account_can_spend(account_id)
        turn_messages = self.build_turn_messages(message, candidate_id, resolved_session_id, account_id)
        route_decision = self._route_message(
            message,
            history=turn_messages[:-1],
            candidate_id=candidate_id,
            account_id=account_id,
            session_id=resolved_session_id,
            root_request_id=root_request_id,
        )
        direct_result = self._execute_direct_route(
            route_decision,
            candidate_id=candidate_id,
            account_id=account_id,
            session_id=resolved_session_id,
            root_request_id=root_request_id,
            turn_started_at=turn_started_at,
        )
        if direct_result is not None:
            self.routing_metrics.record_decision(route_decision, direct_executed=True)
            return direct_result
        self.routing_metrics.record_decision(route_decision, direct_executed=False)
        main_agent_started_at = time.monotonic()
        messages, tool_outputs, reply, streamed_usage = self._collect_main_agent_stream(
            turn_messages,
            account_id=account_id,
            candidate_id=candidate_id,
            session_id=resolved_session_id,
            use_tool_llm=use_tool_llm,
            auto_rag=auto_rag,
            root_request_id=root_request_id,
        )
        usage = self.model_gateway.record_chat_usage_summary(
            self.model_gateway.new_call_context(
                "agent_chat",
                account_id=account_id,
                candidate_id=candidate_id,
                session_id=resolved_session_id,
                root_request_id=root_request_id,
                call_id=f"{root_request_id}-agent_chat-aggregated",
                authorize_spend=False,
            ),
            streamed_usage,
        )
        chat_result = AgentChatResult(
            reply=reply,
            candidate_id=candidate_id,
            session_id=resolved_session_id,
            mode="langchain_agent",
            used_tools=collect_used_tools(messages),
            tool_outputs=tool_outputs,
            usage=merge_usage_summaries(route_decision.usage if route_decision else {}, usage),
            root_request_id=root_request_id,
            routing=build_routing_summary(
                route_decision,
                main_agent_used=True,
                direct_executed=False,
                downstream_latency_ms=elapsed_ms(main_agent_started_at),
                total_latency_ms=elapsed_ms(turn_started_at),
            ),
        )
        return chat_result

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

        turn_started_at = time.monotonic()
        resolved_session_id = session_id or default_session_id(candidate_id, account_id)
        root_request_id = root_request_id or uuid.uuid4().hex
        if account_id is not None:
            self.app.store.assert_account_can_spend(account_id)
        turn_messages = self.build_turn_messages(message, candidate_id, resolved_session_id, account_id)
        route_decision = self._route_message(
            message,
            history=turn_messages[:-1],
            candidate_id=candidate_id,
            account_id=account_id,
            session_id=resolved_session_id,
            root_request_id=root_request_id,
        )
        direct_result = self._execute_direct_route(
            route_decision,
            candidate_id=candidate_id,
            account_id=account_id,
            session_id=resolved_session_id,
            root_request_id=root_request_id,
            turn_started_at=turn_started_at,
        )
        if direct_result is not None:
            self.routing_metrics.record_decision(route_decision, direct_executed=True)
            yield {"type": "step_started", "name": direct_result.used_tools[0]}
            yield {
                "type": "step_completed",
                "name": direct_result.used_tools[0],
                "data": direct_result.tool_outputs[0].get("data"),
            }
            yield {"type": "token", "content": direct_result.reply}
            yield {"type": "final", "result": direct_result}
            return
        self.routing_metrics.record_decision(route_decision, direct_executed=False)
        main_agent_started_at = time.monotonic()
        tool_and_final_messages: list[BaseMessage] = []
        streamed_reply_parts: list[str] = []
        streamed_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        active_tool_names: set[str] = set()

        try:
            for stream_item in self.graph.stream(
                {"messages": turn_messages},
                config={
                    "configurable": {
                        "thread_id": scoped_thread_id(
                            account_id,
                            candidate_id,
                            resolved_session_id,
                        )
                    },
                    "metadata": {"account_id": account_id},
                },
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
        except Exception:
            # 工具已经成功写入后，最后一次模型调用只负责组织自然语言收尾；
            # 收尾超时不能回滚已经提交的业务结果，也不能把整轮任务标为失败。
            tool_outputs = collect_tool_outputs(tool_and_final_messages)
            reply = fallback_reply_from_tool_outputs(tool_outputs)
            if reply is None:
                raise
        else:
            tool_outputs = collect_tool_outputs(tool_and_final_messages)
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
                authorize_spend=False,
            ),
            streamed_usage,
        )
        chat_result = AgentChatResult(
            reply=reply,
            candidate_id=candidate_id,
            session_id=resolved_session_id,
            mode="langchain_agent",
            used_tools=collect_used_tools(tool_and_final_messages),
            tool_outputs=tool_outputs,
            usage=merge_usage_summaries(route_decision.usage if route_decision else {}, usage),
            root_request_id=root_request_id,
            routing=build_routing_summary(
                route_decision,
                main_agent_used=True,
                direct_executed=False,
                downstream_latency_ms=elapsed_ms(main_agent_started_at),
                total_latency_ms=elapsed_ms(turn_started_at),
            ),
        )
        yield {"type": "final", "result": chat_result}

    def _route_message(
        self,
        message: str,
        *,
        history: list[BaseMessage],
        candidate_id: int | None,
        account_id: int | None,
        session_id: str,
        root_request_id: str,
    ) -> IntentDecision | None:
        """在主 Agent 前尝试轻量路由；路由器异常时保持原有流程。"""

        try:
            return self.intent_router.route(
                message,
                history=history,
                candidate_id=candidate_id,
                account_id=account_id,
                session_id=session_id,
                root_request_id=root_request_id,
            )
        except Exception:  # noqa: BLE001 - 路由器是优化层，失败不能阻塞主 Agent。
            return IntentDecision(
                decision_source="router_boundary",
                fallback_reason="router_error",
            )

    def _execute_direct_route(
        self,
        route_decision: IntentDecision | None,
        *,
        candidate_id: int | None,
        account_id: int | None,
        session_id: str,
        root_request_id: str,
        turn_started_at: float,
    ) -> AgentChatResult | None:
        """执行高置信度只读路由；不符合条件时返回 None 进入主 Agent。"""

        if route_decision is None or route_decision.route != "direct_tool":
            return None
        direct_started_at = time.monotonic()
        try:
            reply, tool_outputs = self.direct_intent_executor.execute(
                route_decision,
                candidate_id=candidate_id,
                account_id=account_id,
                session_id=session_id,
                root_request_id=root_request_id,
            )
        except Exception:  # noqa: BLE001 - 直接执行失败时回退到原有工具循环。
            route_decision.fallback_reason = "direct_execution_error"
            return None
        return AgentChatResult(
            reply=reply,
            candidate_id=candidate_id,
            session_id=session_id,
            mode="intent_router_direct",
            used_tools=[str(route_decision.tool_name)],
            tool_outputs=tool_outputs,
            usage=route_decision.usage,
            root_request_id=root_request_id,
            routing=build_routing_summary(
                route_decision,
                main_agent_used=False,
                direct_executed=True,
                downstream_latency_ms=elapsed_ms(direct_started_at),
                total_latency_ms=elapsed_ms(turn_started_at),
            ),
        )

    def _collect_main_agent_stream(
        self,
        turn_messages: list[BaseMessage],
        *,
        account_id: int | None,
        candidate_id: int | None,
        session_id: str,
        use_tool_llm: bool,
        auto_rag: bool,
        root_request_id: str,
    ) -> tuple[list[BaseMessage], list[dict[str, object]], str, dict[str, int]]:
        """收集主 Agent 流并在收尾模型失败时保留已完成的工具结果。"""

        messages: list[BaseMessage] = []
        reply_parts: list[str] = []
        usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        try:
            for stream_item in self.graph.stream(
                {"messages": turn_messages},
                config={
                    "configurable": {
                        "thread_id": scoped_thread_id(account_id, candidate_id, session_id)
                    },
                    "metadata": {"account_id": account_id},
                },
                context={
                    "candidate_id": candidate_id,
                    "account_id": account_id,
                    "session_id": session_id,
                    "use_tool_llm": use_tool_llm and self.tool_llm_available,
                    "default_auto_rag": auto_rag,
                    "root_request_id": root_request_id,
                },
                stream_mode="messages",
            ):
                streamed_message = unpack_stream_message(stream_item)
                if streamed_message is None:
                    continue
                if isinstance(streamed_message, AIMessageChunk):
                    merge_usage(usage, extract_usage_metadata(streamed_message))
                    token = extract_stream_token(streamed_message)
                    if token:
                        reply_parts.append(token)
                    continue
                messages.append(streamed_message)
                if isinstance(streamed_message, AIMessage) and not streamed_message.tool_calls:
                    token = extract_stream_token(streamed_message)
                    if token:
                        reply_parts.append(token)
        except Exception:
            tool_outputs = collect_tool_outputs(messages)
            reply = fallback_reply_from_tool_outputs(tool_outputs)
            if reply is None:
                raise
            return messages, tool_outputs, reply, usage

        tool_outputs = collect_tool_outputs(messages)
        reply = extract_final_reply(messages)
        if reply.startswith("本轮没有生成") and reply_parts:
            reply = "".join(reply_parts).strip()
        return messages, tool_outputs, reply, usage

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

        `database` 后端每轮都读取 PostgreSQL 历史，因此 Web 副本切换后仍能恢复同一
        会话。`memory` 后端只用于本地测试或明确接受单进程状态的开发场景。
        """

        if not self.memory_settings.enabled or candidate_id is None:
            return []
        if self.memory_settings.checkpoint_backend == "database":
            records = self.app.list_chat_messages(
                candidate_id,
                session_id,
                limit=self.memory_settings.restore_history_limit,
                account_id=account_id,
            )
            return build_restored_context_messages(records, self.memory_settings)
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
    """兼容旧调用方：从统一注册表生成 LangChain 工具。"""

    return list(build_langchain_tools(build_job_hunting_tool_registry(app)))


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


def merge_usage_summaries(
    first: dict[str, int | str],
    second: dict[str, int | str],
) -> dict[str, int | str]:
    """合并路由模型和主 Agent 的用量摘要。"""

    result: dict[str, int | str] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        result[key] = int(first.get(key, 0) or 0) + int(second.get(key, 0) or 0)
    sources = {str(first.get("usage_source", "")), str(second.get("usage_source", ""))}
    sources.discard("")
    if "provider" in sources:
        result["usage_source"] = "provider"
    elif "missing" in sources:
        result["usage_source"] = "missing"
    elif sources:
        result["usage_source"] = next(iter(sources))
    return result


def elapsed_ms(started_at: float) -> int:
    """返回单调时钟测得的非负毫秒耗时。"""

    return max(0, round((time.monotonic() - started_at) * 1000))


def build_routing_summary(
    decision: IntentDecision | None,
    *,
    main_agent_used: bool,
    direct_executed: bool,
    downstream_latency_ms: int,
    total_latency_ms: int,
) -> dict[str, object]:
    """构造可持久化的低敏路由观测，不包含消息或模型原始输出。"""

    return {
        "router_active": decision is not None,
        "model_attempted": bool(decision and decision.model_attempted),
        "decision_source": decision.decision_source if decision else "disabled",
        "selected_route": decision.route if decision else "agent",
        "tool_name": decision.tool_name if decision else None,
        "confidence": round(decision.confidence, 4) if decision else 0.0,
        "fallback_reason": decision.fallback_reason if decision else "router_disabled",
        "router_latency_ms": decision.latency_ms if decision else 0,
        "main_agent_used": main_agent_used,
        "direct_executed": direct_executed,
        "downstream_latency_ms": max(0, downstream_latency_ms),
        "total_latency_ms": max(0, total_latency_ms),
    }


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
    status = str(getattr(message, "status", "success") or "success")
    parsed = try_parse_json(raw_content)
    if isinstance(parsed, dict) and parsed.get("status") in {
        "success",
        "queued",
        "rejected",
        "failed",
    }:
        item: dict[str, object] = {
            "tool_name": message.name or "unknown_tool",
            "raw_content": raw_content,
            "status": str(parsed["status"]),
            "data": parsed.get("data"),
        }
        if isinstance(parsed.get("error"), dict):
            item["error"] = parsed["error"]
        if isinstance(parsed.get("meta"), dict):
            item["meta"] = parsed["meta"]
        return item
    item: dict[str, object] = {
        "tool_name": message.name or "unknown_tool",
        "raw_content": raw_content,
        "status": status,
    }
    if status == "error":
        if isinstance(parsed, dict) and parsed.get("error"):
            item["data"] = parsed
        else:
            item["data"] = {"error": raw_content or "工具调用失败。"}
    elif parsed is not None:
        item["data"] = parsed
    return item


def fallback_reply_from_tool_outputs(tool_outputs: list[dict[str, object]]) -> str | None:
    """从已成功的工具结果生成模型收尾失败时的业务回复。"""

    if not tool_outputs:
        return None
    if any(
        item.get("status") in {"failed", "rejected", "error"}
        or (
            isinstance(item.get("data"), dict)
            and bool(item["data"].get("error"))
        )
        for item in tool_outputs
        if isinstance(item, dict)
    ):
        return None

    for item in reversed(tool_outputs):
        if not isinstance(item, dict) or item.get("status") not in {"success", "queued"}:
            continue
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        reply = data.get("reply")
        if isinstance(reply, str) and reply.strip():
            return reply.strip()

    return None


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
