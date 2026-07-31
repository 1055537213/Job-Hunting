"""Agent 对话记忆恢复与启动压缩。

这个模块只处理“聊天历史如何重新进入 LangChain 上下文”。它不改变候选人档案、
RAG 索引或职位匹配事实源；长期事实仍然分别由 SQLite 结构化表和 `long_texts` 管理。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.messages.utils import count_tokens_approximately

from .config import AgentMemorySettings
from .models import ChatMessageRecord


RESTORED_HISTORY_SOURCE = "persistent_chat_history"
RESTORED_SUMMARY_SOURCE = "persistent_chat_history_summary"


def build_restored_context_messages(
    records: list[ChatMessageRecord],
    settings: AgentMemorySettings,
) -> list[BaseMessage]:
    """把 SQLite 聊天历史恢复成可传给 LangChain Agent 的消息列表。

    如果历史过长，会先把较早消息压缩成一条摘要，再保留最近若干条原文消息。
    这样服务重启后既能恢复上下文，又不会一启动就把模型上下文塞满。
    """

    if not settings.enabled:
        return []
    messages = chat_records_to_langchain_messages(records)
    if not messages:
        return []
    if count_tokens_approximately(messages) < settings.restore_trigger_tokens:
        return messages
    return compact_restored_messages(messages, settings)


def chat_records_to_langchain_messages(records: list[ChatMessageRecord]) -> list[BaseMessage]:
    """把持久化聊天记录转换为 LangChain 消息对象。"""

    messages: list[BaseMessage] = []
    for record in records:
        if not record.content.strip():
            continue
        if record.role == "user":
            messages.append(
                HumanMessage(
                    content=record.content,
                    additional_kwargs={"lc_source": RESTORED_HISTORY_SOURCE, "chat_message_id": record.id},
                )
            )
        elif record.role == "assistant":
            messages.append(
                AIMessage(
                    content=record.content,
                    additional_kwargs={"lc_source": RESTORED_HISTORY_SOURCE, "chat_message_id": record.id},
                )
            )
    return messages


def compact_restored_messages(
    messages: list[BaseMessage],
    settings: AgentMemorySettings,
) -> list[BaseMessage]:
    """压缩启动恢复的历史消息，保留最近消息原文。"""

    if len(messages) <= settings.restore_keep_messages:
        return messages
    older_messages = messages[: -settings.restore_keep_messages]
    recent_messages = messages[-settings.restore_keep_messages :]
    return [build_restored_summary_message(older_messages, settings.restore_summary_chars), *recent_messages]


def build_restored_summary_message(messages: list[BaseMessage], max_chars: int) -> HumanMessage:
    """把较早聊天历史整理成一条“不是新请求”的摘要消息。"""

    summary = build_extractive_summary(messages, max_chars)
    return HumanMessage(
        content=(
            "以下是从 SQLite 持久化聊天历史恢复的压缩上下文，不是用户的新请求。\n"
            "后续回答时请把它当作历史背景参考，但不要把摘要里的内容当成未经确认的新事实。\n\n"
            f"{summary}"
        ),
        additional_kwargs={"lc_source": RESTORED_SUMMARY_SOURCE},
    )


def build_extractive_summary(messages: list[BaseMessage], max_chars: int) -> str:
    """生成保守的抽取式摘要。

    这里不调用 LLM，避免服务刚启动恢复历史时就额外消耗模型调用。运行中的长上下文
    会交给 LangChain `SummarizationMiddleware` 使用真实模型做更高质量总结。
    """

    lines: list[str] = []
    for index, message in enumerate(messages, start=1):
        content = message_content_to_text(message).strip()
        if not content:
            continue
        role = "用户" if isinstance(message, HumanMessage) else "助手"
        lines.append(f"{index}. {role}：{single_line(content)}")

    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 24)].rstrip() + "\n...（较早历史已截断）"


def message_content_to_text(message: BaseMessage) -> str:
    """把 LangChain 消息内容压平成普通文本。"""

    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item) for item in content)
    return str(content)


def single_line(text: str) -> str:
    """把多行聊天记录折成一行，减少启动摘要噪声。"""

    return " ".join(part.strip() for part in text.splitlines() if part.strip())
