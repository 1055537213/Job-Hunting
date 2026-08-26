"""轻量意图路由模块。

路由器只负责提出一个受限的路由建议，不直接操作数据库，也不替代主 Agent 的
多步骤规划。高置信度的只读请求可以交给 ``DirectIntentExecutor``，其余请求
统一回退到现有 LangChain Agent。
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol, Sequence

from langchain_core.messages import BaseMessage

from .config import IntentRouterSettings
from .llm import LLMClient
from .model_gateway import ModelGateway

RouteKind = Literal["direct_tool", "agent"]

# 生产 LangChain 客户端走可取消的异步 HTTP；这个上限只保护同步测试替身或自定义
# 客户端，避免超时后仍在运行的不可取消线程无限堆积。
_SYNC_ROUTER_SLOTS = threading.BoundedSemaphore(value=8)

DIRECT_TOOL_NAMES = frozenset(
    {
        "get_current_candidate_profile",
        "list_candidate_profiles",
        "list_imported_jobs",
        "match_all_jobs_for_candidate",
        "list_project_cards_for_candidate",
        "search_candidate_evidence",
    }
)

ROUTER_LATENCY_BUCKETS_MS = (50, 100, 250, 500, 1000, 2000, 3000)
ROUTER_FALLBACK_REASONS = frozenset(
    {
        "router_disabled",
        "empty_message",
        "ambiguous_reference",
        "multi_step",
        "mutation_or_confirmation",
        "invalid_response",
        "model_selected_agent",
        "tool_not_allowed",
        "low_confidence",
        "candidate_missing",
        "query_missing",
        "router_error",
        "router_timeout",
        "router_busy",
        "direct_execution_error",
    }
)

# 这些表达说明当前消息依赖隐含上下文、包含多个步骤，或可能改变业务事实。
# 命中时不调用轻量模型，直接交给主 Agent，避免把模型自报置信度当成安全依据。
AMBIGUOUS_REFERENCE_PATTERN = re.compile(
    r"(?:继续|接着|刚才|上次|这个也|那个也|同样处理|照旧|按之前|和之前一样|"
    r"这个职位|那个职位|这份简历|那份简历|这个文件|那个文件)"
)
MULTI_STEP_PATTERN = re.compile(
    r"(?:然后|并且|同时|顺便|接下来|之后再|完成后再|再帮我|再替我|再给我)"
)
MUTATION_OR_CONFIRMATION_PATTERN = re.compile(
    r"(?:改成|改为|换成|调整为|更新|删除|清空|添加|增加|上传|生成|改写|润色|"
    r"确认|取消|提交|保存一下|记录一下|帮我保存|帮我记录|请保存|请记录|"
    r"(?<!已)(?<!已经)导入|简历|HR|GitHub|github|熟练度|措辞|拔高|提高到|改成精通)"
)


@dataclass
class IntentDecision:
    """轻量路由模型的受限输出。"""

    route: RouteKind = "agent"
    tool_name: str | None = None
    arguments: dict[str, object] = field(default_factory=dict)
    confidence: float = 0.0
    usage: dict[str, int | str] = field(default_factory=dict)
    model_attempted: bool = False
    decision_source: str = "disabled"
    fallback_reason: str | None = None
    latency_ms: int = 0


class IntentRouterProtocol(Protocol):
    """JobHuntingAgent 使用的最小路由接口，便于注入测试替身。"""

    def route(
        self,
        message: str,
        *,
        history: Sequence[BaseMessage] = (),
        candidate_id: int | None,
        account_id: int | None,
        session_id: str,
        root_request_id: str,
    ) -> IntentDecision | None: ...


class IntentRouterTimeoutError(TimeoutError):
    """轻量路由调用超过端到端总截止时间。"""


class IntentRouterBusyError(RuntimeError):
    """不可取消的同步路由调用已达到有界并发上限。"""


class IntentRoutingMetrics:
    """进程内低基数路由指标；不保存消息、账号或请求标识。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._direct_total = 0
        self._fallback_total = 0
        self._timeout_total = 0
        self._fallback_reason_counts: dict[str, int] = {
            reason: 0 for reason in sorted(ROUTER_FALLBACK_REASONS)
        }
        self._latency_bucket_counts: dict[int, int] = {
            boundary: 0 for boundary in ROUTER_LATENCY_BUCKETS_MS
        }
        self._latency_count = 0
        self._latency_sum_ms = 0

    def record_decision(
        self,
        decision: IntentDecision | None,
        *,
        direct_executed: bool,
    ) -> None:
        """在最终路由路径确定时记账，不依赖下游主 Agent 是否成功。"""

        model_attempted = bool(decision and decision.model_attempted)
        reason = normalize_fallback_reason(
            decision.fallback_reason if decision else "router_disabled"
        )
        latency_ms = non_negative_int(decision.latency_ms if decision else 0)
        with self._lock:
            if direct_executed:
                self._direct_total += 1
            else:
                self._fallback_total += 1
                self._fallback_reason_counts[reason] += 1
                if reason == "router_timeout":
                    self._timeout_total += 1
            if model_attempted:
                self._latency_count += 1
                self._latency_sum_ms += latency_ms
                for boundary in ROUTER_LATENCY_BUCKETS_MS:
                    if latency_ms <= boundary:
                        self._latency_bucket_counts[boundary] += 1

    def snapshot(self) -> dict[str, object]:
        """返回 Prometheus 格式化器可安全导出的计数快照。"""

        with self._lock:
            return {
                "direct_total": self._direct_total,
                "fallback_total": self._fallback_total,
                "timeout_total": self._timeout_total,
                "fallback_reason_counts": dict(self._fallback_reason_counts),
                "latency_bucket_counts_ms": dict(self._latency_bucket_counts),
                "latency_count": self._latency_count,
                "latency_sum_ms": self._latency_sum_ms,
            }


class IntentRouter:
    """调用轻量模型生成路由建议，并对结果做本地白名单校验。"""

    def __init__(
        self,
        model_gateway: ModelGateway,
        settings: IntentRouterSettings | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.model_gateway = model_gateway
        if settings is not None:
            self.settings = settings
        else:
            try:
                self.settings = model_gateway.intent_router_settings
            except (TypeError, ValueError):
                # 路由器是优化层，配置错误时必须回退到现有主 Agent。
                self.settings = IntentRouterSettings(enabled=False)
        self._llm_client = llm_client

    def route(
        self,
        message: str,
        *,
        history: Sequence[BaseMessage] = (),
        candidate_id: int | None,
        account_id: int | None,
        session_id: str,
        root_request_id: str,
    ) -> IntentDecision | None:
        """生成路由建议；配置关闭时返回 ``None``，不改变原有 Agent 行为。"""

        if not self.settings.enabled or self.settings.llm is None:
            return None
        guard_reason = agent_fallback_reason(message)
        if guard_reason is not None:
            return IntentDecision(
                decision_source="guard",
                fallback_reason=guard_reason,
            )

        started_at = time.monotonic()
        usage: dict[str, int | str] = {}
        try:
            client = self._llm_client
            if client is None:
                context = self.model_gateway.new_call_context(
                    "intent_router",
                    account_id=account_id,
                    candidate_id=candidate_id,
                    session_id=session_id,
                    root_request_id=root_request_id,
                )
                client = self.model_gateway.llm_client(
                    context,
                    llm_settings=self.settings.llm,
                    usage_sink=usage.update,
                )
            remaining_seconds = self.settings.hard_timeout_seconds - (
                time.monotonic() - started_at
            )
            if remaining_seconds <= 0:
                raise IntentRouterTimeoutError("轻量意图路由初始化超过总截止时间。")
            prompt = build_intent_router_prompt(
                message,
                history=(
                    history[-self.settings.history_messages :]
                    if self.settings.history_messages
                    else ()
                ),
                candidate_id=candidate_id,
            )
            raw_text = complete_with_hard_timeout(
                client,
                prompt,
                timeout_seconds=remaining_seconds,
            )
            decision = parse_intent_decision(
                raw_text,
                confidence_threshold=self.settings.confidence_threshold,
                candidate_id=candidate_id,
            )
            decision.model_attempted = True
            decision.decision_source = "model"
        except IntentRouterTimeoutError:
            decision = IntentDecision(
                model_attempted=True,
                decision_source="model",
                fallback_reason="router_timeout",
            )
        except IntentRouterBusyError:
            decision = IntentDecision(
                decision_source="capacity_guard",
                fallback_reason="router_busy",
            )
        except Exception:  # noqa: BLE001 - 路由器失败必须回退主 Agent。
            decision = IntentDecision(
                model_attempted=True,
                decision_source="model",
                fallback_reason="router_error",
            )
        decision.latency_ms = max(0, round((time.monotonic() - started_at) * 1000))
        # 同步兼容客户端超时后可能在守护线程中迟到完成；这里只保存截止时刻的快照，
        # 避免已经返回的 AgentChatResult 被后台线程继续修改。
        decision.usage = dict(usage)
        return decision


class DirectIntentExecutor:
    """执行路由器允许的只读工具，并返回 Agent 兼容的摘要。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    def execute(
        self,
        decision: IntentDecision,
        *,
        candidate_id: int | None,
        account_id: int | None,
        session_id: str,
        root_request_id: str,
    ) -> tuple[str, list[dict[str, object]]]:
        """执行一个已通过白名单校验的只读路由。"""

        tool_name = decision.tool_name
        if decision.route != "direct_tool" or tool_name not in DIRECT_TOOL_NAMES:
            raise ValueError("当前路由不是可直接执行的只读工具。")

        if tool_name == "get_current_candidate_profile":
            if candidate_id is None:
                raise ValueError("读取当前候选人档案需要候选人 ID。")
            data = asdict(self.app.get_candidate_profile(candidate_id, account_id=account_id))
            reply = f"当前候选人档案是“{data.get('name') or '未命名候选人'}”，已读取完成。"
        elif tool_name == "list_candidate_profiles":
            profiles = self.app.list_candidate_profiles(account_id=account_id)
            data = {"profiles": [asdict(profile) for profile in profiles]}
            reply = f"当前账号共有 {len(profiles)} 个候选人档案。"
        elif tool_name == "list_imported_jobs":
            jobs = self.app.list_jobs(account_id=account_id)
            data = {"jobs": [asdict(job) for job in jobs]}
            reply = f"当前职位池共有 {len(jobs)} 个已导入职位。"
        elif tool_name == "match_all_jobs_for_candidate":
            if candidate_id is None:
                raise ValueError("匹配职位需要候选人 ID。")
            matches = self.app.match_all_jobs(candidate_id, account_id=account_id)
            data = {"candidate_id": candidate_id, "matches": [asdict(match) for match in matches]}
            reply = f"已完成职位匹配，共分析 {len(matches)} 个职位。"
        elif tool_name == "list_project_cards_for_candidate":
            if candidate_id is None:
                raise ValueError("读取项目经历卡片需要候选人 ID。")
            cards = self.app.list_project_cards(candidate_id, account_id=account_id)
            data = {"project_cards": [asdict(card) for card in cards]}
            reply = f"当前共有 {len(cards)} 张项目经历卡片。"
        else:
            query = str(decision.arguments.get("query") or "").strip()
            top_k = _bounded_int(decision.arguments.get("top_k"), default=5, minimum=1, maximum=10)
            if candidate_id is None or not query:
                raise ValueError("检索候选人证据需要候选人 ID 和查询内容。")
            results = self.app.search_rag(
                query,
                top_k=top_k,
                entity_types=_string_list_or_none(decision.arguments.get("entity_types")),
                account_id=account_id,
                candidate_id=candidate_id,
                session_id=session_id,
                root_request_id=root_request_id,
            )
            data = {"query": query, "results": [asdict(result) for result in results]}
            reply = f"已检索候选人证据，找到 {len(results)} 条相关材料。"

        return reply, [{"tool_name": tool_name, "status": "success", "data": data}]


def build_intent_router_prompt(
    message: str,
    *,
    history: Sequence[BaseMessage] = (),
    candidate_id: int | None,
) -> str:
    """构造短输入、强约束的路由提示词。"""

    history_text = "\n".join(
        f"{getattr(item, 'type', 'message')}: {_message_text(item)[:1200]}"
        for item in history
        if _message_text(item).strip()
    )
    return f"""
你是求职助手的轻量意图路由器。你不能回答用户问题，也不能执行工具。
你的任务只有一个：判断当前消息是否可以直接交给一个只读工具；无法确定时必须返回 agent。

允许 direct_tool 的工具：
- get_current_candidate_profile：读取当前候选人档案
- list_candidate_profiles：列出候选人档案
- list_imported_jobs：列出已导入职位
- match_all_jobs_for_candidate：匹配当前候选人与职位池
- list_project_cards_for_candidate：列出项目经历卡片
- search_candidate_evidence：检索候选人证据，arguments 必须包含 query

以下情况必须返回 agent：保存或修改资料、导入职位、生成或改写简历、HR 回复、GitHub 分析、
涉及确认/真实性边界的请求、多步骤请求、无法从当前消息和历史确定参数的请求。
职位匹配只读取当前候选人和已导入职位；candidate_id 存在且用户明确要求匹配职位池时，
应返回 direct_tool 和 match_all_jobs_for_candidate，不要因为“匹配”包含计算而回退 agent。

当前候选人 ID：{candidate_id}
最近对话：
{history_text or "（无）"}

当前用户消息：
{message}

只返回 JSON，不要 Markdown：
{{
  "route": "direct_tool" 或 "agent",
  "tool_name": null 或允许列表中的工具名,
  "arguments": {{}},
  "confidence": 0.0
}}
""".strip()


def parse_intent_decision(
    text: str,
    *,
    confidence_threshold: float,
    candidate_id: int | None,
) -> IntentDecision:
    """解析并限制模型路由输出。"""

    data = json.loads(_extract_json_object(text))
    if not isinstance(data, dict):
        return IntentDecision(fallback_reason="invalid_response")
    raw_confidence = data.get("confidence", 0.0)
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    tool_name = data.get("tool_name")
    arguments = data.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    if data.get("route") != "direct_tool":
        return IntentDecision(confidence=confidence, fallback_reason="model_selected_agent")
    if not isinstance(tool_name, str) or tool_name not in DIRECT_TOOL_NAMES:
        return IntentDecision(confidence=confidence, fallback_reason="tool_not_allowed")
    if confidence < confidence_threshold:
        return IntentDecision(confidence=confidence, fallback_reason="low_confidence")
    if _requires_candidate(tool_name) and candidate_id is None:
        return IntentDecision(confidence=confidence, fallback_reason="candidate_missing")
    if tool_name == "search_candidate_evidence" and not str(arguments.get("query") or "").strip():
        return IntentDecision(confidence=confidence, fallback_reason="query_missing")
    return IntentDecision(
        route="direct_tool",
        tool_name=str(tool_name),
        arguments=arguments,
        confidence=confidence,
    )


def agent_fallback_reason(message: str) -> str | None:
    """返回确定性回退原因；安全的显式只读消息返回 ``None``。"""

    normalized = " ".join(message.split()).strip()
    if not normalized:
        return "empty_message"
    checks = (
        (AMBIGUOUS_REFERENCE_PATTERN, "ambiguous_reference"),
        (MULTI_STEP_PATTERN, "multi_step"),
        (MUTATION_OR_CONFIRMATION_PATTERN, "mutation_or_confirmation"),
    )
    for pattern, reason in checks:
        if pattern.search(normalized):
            return reason
    return None


def requires_agent_fallback(message: str) -> bool:
    """判断消息是否必须绕过轻量模型进入主 Agent。"""

    return agent_fallback_reason(message) is not None


def normalize_fallback_reason(value: object) -> str:
    """把任意兼容路由器原因收束到固定标签集合。"""

    reason = str(value or "router_error")
    return reason if reason in ROUTER_FALLBACK_REASONS else "router_error"


def non_negative_int(value: object) -> int:
    """把指标输入转换成非负整数，拒绝布尔值冒充耗时。"""

    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def complete_with_hard_timeout(
    client: LLMClient,
    prompt: str,
    *,
    timeout_seconds: float,
) -> str:
    """在总截止时间内完成路由调用，优先使用可取消的异步模型接口。"""

    async_complete = getattr(client, "acomplete", None)
    if callable(async_complete):
        return _run_async_completion(
            lambda: async_complete(prompt),
            timeout_seconds=timeout_seconds,
        )
    return _run_sync_completion(
        lambda: client.complete(prompt),
        timeout_seconds=timeout_seconds,
    )


def _run_async_completion(
    completion_factory: Callable[[], Awaitable[str]],
    *,
    timeout_seconds: float,
) -> str:
    """从同步 Agent 边界运行可取消协程，并转换成稳定的路由超时异常。"""

    async def wait_for_completion() -> str:
        try:
            return await asyncio.wait_for(completion_factory(), timeout=timeout_seconds)
        except TimeoutError as error:
            raise IntentRouterTimeoutError("轻量意图路由超过总截止时间。") from error

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(wait_for_completion())
    # 当前路由接口是同步的；若未来从事件循环线程直接调用，则放入有界守护线程，
    # 避免嵌套 asyncio.run，同时仍按同一总截止时间返回。
    return _run_sync_completion(
        lambda: asyncio.run(wait_for_completion()),
        timeout_seconds=timeout_seconds,
    )


def _run_sync_completion(
    completion: Callable[[], str],
    *,
    timeout_seconds: float,
) -> str:
    """为没有异步接口的兼容客户端提供有界守护线程截止时间。"""

    if not _SYNC_ROUTER_SLOTS.acquire(blocking=False):
        raise IntentRouterBusyError("同步路由调用已达到并发上限。")
    completed = threading.Event()
    result: dict[str, object] = {}

    def invoke() -> None:
        try:
            result["value"] = completion()
        except Exception as error:  # noqa: BLE001 - 在调用线程恢复原异常。
            result["error"] = error
        finally:
            _SYNC_ROUTER_SLOTS.release()
            completed.set()

    threading.Thread(target=invoke, name="intent-router-call", daemon=True).start()
    if not completed.wait(timeout_seconds):
        raise IntentRouterTimeoutError("轻量意图路由超过总截止时间。")
    error = result.get("error")
    if isinstance(error, Exception):
        raise error
    value = result.get("value")
    if not isinstance(value, str):
        raise TypeError("轻量意图路由没有返回文本。")
    return value


def _requires_candidate(tool_name: object) -> bool:
    return tool_name in {
        "get_current_candidate_profile",
        "match_all_jobs_for_candidate",
        "list_project_cards_for_candidate",
        "search_candidate_evidence",
    }


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _string_list_or_none(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result = [str(item).strip() for item in value if str(item).strip()]
    return result or None


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end < start:
        raise ValueError("路由模型没有返回 JSON 对象。")
    return stripped[start : end + 1]


def _message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, list):
        return "\n".join(str(item) for item in content)
    return str(content)
