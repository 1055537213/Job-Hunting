"""把内部工具目录适配成 MCP 兼容结构，不引入 MCP Server 运行时。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from .tool_registry import ToolMetadata, ToolResult

_TOOL_RESULT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["success", "queued", "rejected", "failed"],
        },
        "data": {
            "anyOf": [
                {"type": "object", "additionalProperties": True},
                {"type": "null"},
            ]
        },
        "error": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "retryable": {"type": "boolean"},
                    },
                    "required": ["code", "message", "retryable"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ]
        },
        "meta": {"type": "object", "additionalProperties": True},
    },
    "required": ["status", "data", "error", "meta"],
    "additionalProperties": False,
}


def build_mcp_tool_definitions(
    catalog: Iterable[ToolMetadata],
    *,
    include_mutating: bool = False,
) -> list[dict[str, Any]]:
    """生成 MCP Tool 定义；默认只暴露只读能力。"""

    definitions: list[dict[str, Any]] = []
    for metadata in catalog:
        if not include_mutating and not metadata.read_only:
            continue
        definitions.append(
            {
                "name": metadata.name,
                "title": metadata.audit_label,
                "description": metadata.description,
                "inputSchema": metadata.input_schema(),
                "outputSchema": deepcopy(_TOOL_RESULT_OUTPUT_SCHEMA),
                "annotations": {
                    "title": metadata.audit_label,
                    "readOnlyHint": metadata.read_only,
                    "destructiveHint": metadata.destructive,
                    "idempotentHint": metadata.idempotent,
                    "openWorldHint": False,
                },
                "execution": {
                    "taskSupport": (
                        "optional"
                        if metadata.execution_mode == "background"
                        else "forbidden"
                    )
                },
            }
        )
    return definitions


def to_mcp_call_result(result: ToolResult) -> dict[str, Any]:
    """把统一结果映射为 MCP ``CallToolResult`` 的核心字段。"""

    structured_content = result.to_payload()
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    structured_content,
                    ensure_ascii=False,
                    default=str,
                ),
            }
        ],
        "structuredContent": structured_content,
        "isError": result.status in {"rejected", "failed"},
    }

