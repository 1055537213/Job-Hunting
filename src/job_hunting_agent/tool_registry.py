"""Agent 工具的统一定义、执行和结果契约。

注册表是 Agent、轻量意图路由和可选协议适配器共同依赖的 seam。业务实现仍由
``JobHuntingApp`` 提供；调用方不再各自维护工具白名单、参数校验和错误格式。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

ToolStatus = Literal["success", "queued", "rejected", "failed"]
ToolExecutionMode = Literal["sync", "background"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """一次工具调用可使用的账号、候选人和链路上下文。"""

    candidate_id: int | None = None
    account_id: int | None = None
    session_id: str = ""
    root_request_id: str = ""
    use_tool_llm: bool = True
    default_auto_rag: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ToolContext:
        """从 LangChain runtime context 构造类型化上下文。"""

        return cls(
            candidate_id=_optional_int(value.get("candidate_id")),
            account_id=_optional_int(value.get("account_id")),
            session_id=str(value.get("session_id") or ""),
            root_request_id=str(value.get("root_request_id") or ""),
            use_tool_llm=bool(value.get("use_tool_llm", True)),
            default_auto_rag=bool(value.get("default_auto_rag", True)),
        )

    def require_candidate_id(self) -> int:
        """返回当前候选人 ID；未绑定候选人时拒绝执行。"""

        if self.candidate_id is None:
            raise ValueError("当前会话还没有绑定候选人档案，请先创建或选择候选人。")
        return self.candidate_id

    def require_account_id(self, message: str = "当前操作缺少账号归属。") -> int:
        """返回当前账号 ID；未绑定账号时拒绝执行。"""

        if self.account_id is None:
            raise ValueError(message)
        return self.account_id


@dataclass(frozen=True, slots=True)
class ToolError:
    """调用方可稳定判断的工具错误。"""

    code: str
    message: str
    retryable: bool = False

    def to_payload(self) -> dict[str, object]:
        """转换为可序列化结构。"""

        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(slots=True)
class ToolResult:
    """所有 Agent 工具共享的结果 envelope。"""

    status: ToolStatus
    data: dict[str, Any] | None = None
    error: ToolError | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, data: dict[str, Any] | None = None) -> ToolResult:
        """构造同步成功结果。"""

        return cls(status="success", data=data or {})

    @classmethod
    def queued(cls, data: dict[str, Any] | None = None) -> ToolResult:
        """构造已排队的长任务结果。"""

        return cls(status="queued", data=data or {})

    @classmethod
    def rejected(cls, code: str, message: str) -> ToolResult:
        """构造参数、权限或业务规则拒绝结果。"""

        return cls(status="rejected", error=ToolError(code=code, message=message))

    @classmethod
    def failed(cls, code: str, message: str, *, retryable: bool = False) -> ToolResult:
        """构造执行失败结果。"""

        return cls(
            status="failed",
            error=ToolError(code=code, message=message, retryable=retryable),
        )

    def to_payload(self) -> dict[str, object]:
        """返回供 LangChain、Web 和 MCP adapter 复用的结构。"""

        return {
            "status": self.status,
            "data": self.data,
            "error": self.error.to_payload() if self.error is not None else None,
            "meta": dict(self.meta),
        }

    def to_json(self) -> str:
        """序列化为兼容 LangChain ToolMessage 的 JSON 文本。"""

        return json.dumps(self.to_payload(), ensure_ascii=False, indent=2, default=str)

    def to_trace_output(self, tool_name: str) -> dict[str, object]:
        """转换成 ``AgentChatResult.tool_outputs`` 使用的结构。"""

        output: dict[str, object] = {
            "tool_name": tool_name,
            "status": self.status,
            "data": self.data,
            "meta": dict(self.meta),
        }
        if self.error is not None:
            output["error"] = self.error.to_payload()
        return output


ToolHandler = Callable[[BaseModel, ToolContext], ToolResult]
DirectReplyBuilder = Callable[[dict[str, Any]], str]


@dataclass(frozen=True, slots=True)
class ToolErrorRule:
    """把一个已知业务异常翻译成稳定错误码。"""

    exception_type: type[Exception]
    code: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    """不含可执行 handler 的工具目录项，供路由、审计和协议发现使用。"""

    name: str
    description: str
    args_model: type[BaseModel]
    audit_label: str
    read_only: bool
    destructive: bool
    idempotent: bool
    direct_route: bool
    requires_candidate: bool
    requires_account: bool
    requires_confirmation: bool
    execution_mode: ToolExecutionMode
    timeout_seconds: float | None
    trace_priority: int

    def input_schema(self) -> dict[str, Any]:
        """返回协议无关的 JSON Schema。"""

        return self.args_model.model_json_schema()


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一个工具的实现和所有调用方需要共享的元数据。"""

    name: str
    description: str
    args_model: type[BaseModel]
    handler: ToolHandler
    audit_label: str
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    direct_route: bool = False
    requires_candidate: bool = False
    requires_account: bool = False
    requires_confirmation: bool = False
    execution_mode: ToolExecutionMode = "sync"
    timeout_seconds: float | None = None
    trace_priority: int = 0
    direct_reply: DirectReplyBuilder | None = None
    error_rules: tuple[ToolErrorRule, ...] = ()

    def __post_init__(self) -> None:
        """拒绝互相矛盾或无法发现的工具定义。"""

        if not self.name.strip():
            raise ValueError("工具名称不能为空。")
        if not self.description.strip():
            raise ValueError(f"工具 {self.name} 缺少描述。")
        if self.direct_route and not self.read_only:
            raise ValueError(f"直达工具 {self.name} 必须是只读工具。")
        if self.direct_route and self.requires_confirmation:
            raise ValueError(f"需要确认的工具 {self.name} 不能进入直达路由。")
        if self.direct_route and self.direct_reply is None:
            raise ValueError(f"直达工具 {self.name} 缺少回复构造器。")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError(f"工具 {self.name} 的超时必须大于 0。")

    def input_schema(self) -> dict[str, Any]:
        """返回可供 LangChain 或 MCP adapter 使用的 JSON Schema。"""

        return self.args_model.model_json_schema()

    def metadata(self) -> ToolMetadata:
        """返回不会意外执行工具的只读目录项。"""

        return ToolMetadata(
            name=self.name,
            description=self.description,
            args_model=self.args_model,
            audit_label=self.audit_label,
            read_only=self.read_only,
            destructive=self.destructive,
            idempotent=self.idempotent,
            direct_route=self.direct_route,
            requires_candidate=self.requires_candidate,
            requires_account=self.requires_account,
            requires_confirmation=self.requires_confirmation,
            execution_mode=self.execution_mode,
            timeout_seconds=self.timeout_seconds,
            trace_priority=self.trace_priority,
        )


class ToolRegistry:
    """通过一个小接口集中工具发现、校验、执行和错误翻译。"""

    def __init__(self, specs: Iterable[ToolSpec]):
        resolved: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in resolved:
                raise ValueError(f"工具名称重复：{spec.name}")
            resolved[spec.name] = spec
        if not resolved:
            raise ValueError("工具注册表不能为空。")
        self._specs = resolved

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._specs

    def get(self, name: str) -> ToolSpec:
        """读取一个已注册工具；未知名称由调用方显式处理。"""

        try:
            return self._specs[name]
        except KeyError as error:
            raise KeyError(f"未知工具：{name}") from error

    def list_specs(self) -> tuple[ToolSpec, ...]:
        """按注册顺序返回不可变工具定义列表。"""

        return tuple(self._specs.values())

    def direct_specs(self) -> tuple[ToolSpec, ...]:
        """返回轻量路由可以安全直达的只读工具。"""

        return tuple(spec for spec in self._specs.values() if spec.direct_route)

    def execute(
        self,
        name: str,
        arguments: Mapping[str, object] | None,
        context: ToolContext,
    ) -> ToolResult:
        """校验参数并执行工具，把所有结果收束为统一 envelope。"""

        started_at = time.monotonic()
        spec = self._specs.get(name)
        if spec is None:
            return _finalize_result(
                ToolResult.rejected("unknown_tool", f"未知工具：{name}"),
                name=name,
                execution_mode="sync",
                started_at=started_at,
            )
        if spec.requires_candidate and context.candidate_id is None:
            result = ToolResult.rejected(
                "candidate_missing",
                "当前会话还没有绑定候选人档案，请先创建或选择候选人。",
            )
            return _finalize_result(
                result,
                name=name,
                execution_mode=spec.execution_mode,
                started_at=started_at,
            )
        if spec.requires_account and context.account_id is None:
            result = ToolResult.rejected("account_missing", "当前操作缺少账号归属。")
            return _finalize_result(
                result,
                name=name,
                execution_mode=spec.execution_mode,
                started_at=started_at,
            )
        try:
            parsed_arguments = spec.args_model.model_validate(dict(arguments or {}))
        except ValidationError as error:
            result = ToolResult.rejected("invalid_arguments", _validation_message(error))
        else:
            try:
                result = spec.handler(parsed_arguments, context)
                if not isinstance(result, ToolResult):
                    raise TypeError(f"工具 {name} 没有返回 ToolResult。")
            except Exception as error:  # noqa: BLE001 - 在注册表统一翻译工具错误。
                result = _translate_tool_error(spec, error)
        return _finalize_result(
            result,
            name=name,
            execution_mode=spec.execution_mode,
            started_at=started_at,
        )


def _finalize_result(
    result: ToolResult,
    *,
    name: str,
    execution_mode: ToolExecutionMode,
    started_at: float,
) -> ToolResult:
    result.meta = {
        **result.meta,
        "tool_name": name,
        "execution_mode": execution_mode,
        "elapsed_ms": max(0, round((time.monotonic() - started_at) * 1000)),
    }
    return result


def _validation_message(error: ValidationError) -> str:
    details = error.errors(include_url=False)
    if not details:
        return "工具参数无效。"
    first = details[0]
    location = ".".join(str(item) for item in first.get("loc", ()))
    message = str(first.get("msg") or "参数无效")
    return f"工具参数 {location or 'arguments'} 无效：{message}"


def _translate_tool_error(spec: ToolSpec, error: Exception) -> ToolResult:
    for rule in spec.error_rules:
        if isinstance(error, rule.exception_type):
            if rule.retryable:
                return ToolResult.failed(rule.code, str(error), retryable=True)
            return ToolResult.rejected(rule.code, str(error))
    if isinstance(error, ValueError):
        return ToolResult.rejected("invalid_request", str(error))
    logger.exception("工具执行失败：%s", spec.name, exc_info=error)
    return ToolResult.failed(
        "internal_error",
        "工具执行失败，请稍后重试。",
        retryable=True,
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("工具上下文中的 ID 必须是整数。")
    return value
