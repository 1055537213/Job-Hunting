"""标准 LangChain Agent 门面。

这个模块把用户可见的聊天入口统一收束为一条标准链路：

Web / CLI
    -> JobHuntingAgent
    -> LangChain create_agent
    -> Tools
    -> JobHuntingApp
    -> SQLite / RAG / LLM

其中：

- SQLite 仍然是结构化事实源。
- long_texts 仍然是长文本材料登记处。
- RAG 仍然只是派生语义索引，不单独充当事实源。
- Agent 不直接改数据库，只能通过工具调用 `JobHuntingApp`。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, TypedDict

from langchain.agents import create_agent
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from .app import JobHuntingApp
from .config import DEFAULT_ENV_PATH, load_llm_settings
from .llm import build_chat_model, build_llm_client, extract_message_text
from .models import AgentChatResult


class JobHuntingAgentContext(TypedDict):
    """LangChain Agent 运行时上下文。

    `candidate_id` 让工具知道当前服务的是哪一个候选人。
    `rag_dir` 告诉工具把增量索引写到哪里。
    `use_tool_llm` 控制工具内部是否继续调用真实大模型。
    `default_auto_rag` 让前端勾选项能传到工具层，而不是写死在 prompt 里。
    """

    candidate_id: int | None
    rag_dir: str
    use_tool_llm: bool
    default_auto_rag: bool


AGENT_SYSTEM_PROMPT = """
你是一个本地运行的求职助手 LangChain Agent。

你的职责：
1. 在当前会话已经绑定候选人档案时，帮用户整理并保存候选人资料。
2. 基于本地已导入职位做匹配分析。
3. 为职位生成职位定制简历草稿。
4. 对本地项目进行分析，并等待候选人确认项目摘要。

你必须遵守这些边界：
- 结构化事实只能通过工具写入 SQLite。
- 长文本材料只能通过工具写入 long_texts，再由工具决定是否增量进入 RAG。
- RAG 检索只是证据索引，不是事实源。
- 不能登录 BOSS、不能爬取网站、不能自动投递、不能自动发送 HR 消息。
- 不要假装已经执行某个保存/导入/匹配动作；只有在工具返回结果后才能确认。
- 如果当前会话还没有绑定候选人，就先用 list_candidate_profiles 帮用户确认当前有哪些档案，
  并明确提醒用户先创建或选择候选人，再继续保存资料。
- 当用户补充资料时，优先调用 ingest_candidate_message。
- 当用户问“适合哪些岗位”时，优先调用 match_all_jobs_for_candidate。
- 当用户让你改简历时，优先调用 create_resume_draft_for_job。

最终回复请使用中文，先说你已经完成了什么，再简洁说明下一步建议。
""".strip()


class JobHuntingAgent:
    """求职助手的标准 LangChain Agent 门面。"""

    def __init__(
        self,
        app: JobHuntingApp,
        env_path: str | Path = DEFAULT_ENV_PATH,
        rag_dir: str | Path = "data/chroma",
        model: BaseChatModel | None = None,
    ):
        """创建一个绑定本地应用服务的 LangChain Agent。

        传入 `model` 时常用于测试：这样可以注入假模型，而不用真的访问网络。
        生产环境则从 `.env` 自动构造 DeepSeek / OpenAI-compatible ChatModel。
        """

        self.app = app
        self.env_path = Path(env_path)
        self.rag_dir = Path(rag_dir)
        self.model = model or build_chat_model(load_llm_settings(self.env_path))
        self.graph = create_agent(
            model=self.model,
            tools=build_job_hunting_tools(app, self.env_path),
            system_prompt=AGENT_SYSTEM_PROMPT,
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
    ) -> AgentChatResult:
        """执行一轮标准 LangChain Agent 对话。"""

        resolved_session_id = session_id or default_session_id(candidate_id)
        result = self.graph.invoke(
            {"messages": [HumanMessage(content=message)]},
            config={"configurable": {"thread_id": resolved_session_id}},
            context={
                "candidate_id": candidate_id,
                "rag_dir": str(self.rag_dir),
                "use_tool_llm": use_tool_llm,
                "default_auto_rag": auto_rag,
            },
        )
        messages = list(result.get("messages", []))
        return AgentChatResult(
            reply=extract_final_reply(messages),
            candidate_id=candidate_id,
            session_id=resolved_session_id,
            mode="langchain_agent",
            used_tools=collect_used_tools(messages),
            tool_outputs=collect_tool_outputs(messages),
        )


def build_job_hunting_tools(app: JobHuntingApp, env_path: Path) -> list[object]:
    """构建标准 LangChain Agent 工具列表。"""

    @tool
    def ingest_candidate_message(
        message: str,
        runtime: ToolRuntime,
        auto_rag: bool | None = None,
    ) -> str:
        """当用户补充候选人资料、技能、项目经历或 HR 对话时，自动保存到 SQLite 和 RAG。"""

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        llm_client = load_tool_llm_client(env_path) if context["use_tool_llm"] else None
        result = app.ingest_conversation_message(
            candidate_id,
            message,
            llm_client=llm_client,
            rag_persist_directory=context["rag_dir"],
            auto_rebuild_rag=context["default_auto_rag"] if auto_rag is None else auto_rag,
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
    def get_current_candidate_profile(runtime: ToolRuntime) -> str:
        """读取当前候选人的结构化档案，用于回答“我现在的档案里有什么”。"""

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        return dumps_tool_output(asdict(app.get_candidate_profile(candidate_id)))

    @tool
    def list_candidate_profiles() -> str:
        """列出本地所有候选人档案，适合在用户不确定当前档案时使用。"""

        return dumps_tool_output({"profiles": [asdict(profile) for profile in app.list_candidate_profiles()]})

    @tool
    def search_candidate_evidence(
        query: str,
        runtime: ToolRuntime,
        top_k: int = 5,
        entity_types: list[str] | None = None,
    ) -> str:
        """从本地 RAG 索引检索候选人证据片段，但不要把结果直接当成新的事实。"""

        context = require_runtime_context(runtime)
        results = app.search_rag(query, context["rag_dir"], top_k, entity_types)
        return dumps_tool_output({"query": query, "results": [asdict(item) for item in results]})

    @tool
    def import_job_from_text(raw_text: str, source_url: str | None = None) -> str:
        """导入用户主动复制回来的职位文本，并解析成标准化职位记录。"""

        job = app.import_job_text(raw_text, source_url)
        return dumps_tool_output({"job": asdict(job)})

    @tool
    def list_imported_jobs() -> str:
        """列出本地已经导入的职位池，供后续匹配或简历改写选择。"""

        return dumps_tool_output({"jobs": [asdict(job) for job in app.list_jobs()]})

    @tool
    def match_all_jobs_for_candidate(runtime: ToolRuntime) -> str:
        """匹配当前候选人与本地全部职位，并返回按推荐顺序排序的结果。"""

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        jobs_by_id = {job.id: job for job in app.list_jobs()}
        matches = app.match_all_jobs(candidate_id)
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
    def list_project_cards_for_candidate(runtime: ToolRuntime) -> str:
        """列出当前候选人的项目经历卡片，查看哪些还待确认。"""

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        cards = app.list_project_cards(candidate_id)
        return dumps_tool_output({"project_cards": [asdict(card) for card in cards]})

    @tool
    def analyze_local_project_for_candidate(
        project_path: str,
        runtime: ToolRuntime,
    ) -> str:
        """分析本地项目目录并保存成待确认项目经历卡片。"""

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        record = app.analyze_project_for_candidate(candidate_id, project_path)
        return dumps_tool_output(asdict(record))

    @tool
    def confirm_project_card(
        record_id: int,
        runtime: ToolRuntime,
        confirmed_summary: str | None = None,
    ) -> str:
        """确认一张项目卡片，并把候选人确认摘要保存为后续可检索证据。

        真实性边界：只有在候选人已经明确确认内容时，才应该调用这个工具。
        它会把“待确认卡片”提升为后续可引用的项目证据，但不会反向覆盖候选人档案。
        """

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        allowed_record_ids = {record.id for record in app.list_project_cards(candidate_id)}
        if record_id not in allowed_record_ids:
            raise ValueError(f"项目卡片 {record_id} 不属于当前候选人 {candidate_id}。")
        record = app.confirm_project_card(record_id, confirmed_summary)
        return dumps_tool_output(asdict(record))

    @tool
    def create_resume_draft_for_job(
        job_id: int,
        runtime: ToolRuntime,
        use_rag: bool = True,
        rag_query: str | None = None,
    ) -> str:
        """为当前候选人生成职位定制简历草稿，并保存成单独版本，不覆盖原档案。"""

        context = require_runtime_context(runtime)
        candidate_id = require_candidate_id(context)
        llm_client = load_tool_llm_client(env_path) if context["use_tool_llm"] else None
        draft = app.create_resume_draft(
            candidate_id,
            job_id,
            llm_client=llm_client,
            rag_persist_directory=context["rag_dir"] if use_rag else None,
            rag_query=rag_query,
        )
        return dumps_tool_output(asdict(draft))

    return [
        ingest_candidate_message,
        get_current_candidate_profile,
        list_candidate_profiles,
        search_candidate_evidence,
        import_job_from_text,
        list_imported_jobs,
        match_all_jobs_for_candidate,
        list_project_cards_for_candidate,
        analyze_local_project_for_candidate,
        confirm_project_card,
        create_resume_draft_for_job,
    ]


def require_runtime_context(
    runtime: ToolRuntime,
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


def load_tool_llm_client(env_path: Path):
    """为工具内部的“单次 prompt”场景构造 LLM 客户端。"""

    return build_llm_client(load_llm_settings(env_path))


def dumps_tool_output(value: dict[str, Any]) -> str:
    """统一序列化工具输出，方便 Agent 阅读，也方便 Web/CLI 再解析。"""

    return json.dumps(value, ensure_ascii=False, indent=2)


def default_session_id(candidate_id: int | None) -> str:
    """生成默认会话 ID。

    这里把候选人 ID 编进会话名，避免多个候选人共用同一段 Agent 历史记忆。
    """

    return f"candidate-{candidate_id or 'unknown'}"


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
        raw_content = stringify_tool_message_content(message.content)
        item: dict[str, object] = {
            "tool_name": message.name or "unknown_tool",
            "raw_content": raw_content,
        }
        parsed = try_parse_json(raw_content)
        if parsed is not None:
            item["data"] = parsed
        outputs.append(item)
    return outputs


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
