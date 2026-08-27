"""可选 MCP 工具定义 adapter 的契约测试。"""

from job_hunting_agent.job_hunting_tools import job_hunting_tool_catalog
from job_hunting_agent.mcp_tool_adapter import (
    build_mcp_tool_definitions,
    to_mcp_call_result,
)
from job_hunting_agent.tool_registry import ToolResult


def test_mcp_adapter_exports_only_read_only_tools_by_default() -> None:
    """对外协议默认采用最小权限，不暴露写入和确认类工具。"""

    definitions = build_mcp_tool_definitions(job_hunting_tool_catalog())

    assert definitions
    assert all(item["annotations"]["readOnlyHint"] is True for item in definitions)
    assert "list_imported_jobs" in {item["name"] for item in definitions}
    assert "import_job_from_text" not in {item["name"] for item in definitions}
    assert all(item["inputSchema"]["type"] == "object" for item in definitions)
    assert all(item["outputSchema"]["type"] == "object" for item in definitions)


def test_mcp_adapter_preserves_structured_result_and_error_state() -> None:
    """MCP 调用结果和内部 ToolResult 使用同一份结构化数据。"""

    result = ToolResult.failed("provider_timeout", "上游超时。", retryable=True)
    result.meta = {"tool_name": "search_candidate_evidence", "elapsed_ms": 3000}

    payload = to_mcp_call_result(result)

    assert payload["isError"] is True
    assert payload["structuredContent"] == result.to_payload()
    assert payload["content"][0]["type"] == "text"
    assert "provider_timeout" in payload["content"][0]["text"]

