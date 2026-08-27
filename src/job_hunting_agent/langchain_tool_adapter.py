"""把内部工具注册表适配成 LangChain 工具。"""

from __future__ import annotations

from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import create_model

from .tool_registry import ToolContext, ToolRegistry, ToolSpec


def build_langchain_tools(registry: ToolRegistry) -> list[BaseTool]:
    """从注册表生成 LangChain 工具，避免重复维护名称和参数 Schema。"""

    return [_build_langchain_tool(registry, spec) for spec in registry.list_specs()]


def _build_langchain_tool(registry: ToolRegistry, spec: ToolSpec) -> BaseTool:
    runtime_args_model = create_model(
        f"{spec.args_model.__name__}WithRuntime",
        __base__=spec.args_model,
        runtime=(ToolRuntime[dict[str, object], Any], ...),
    )

    def invoke(
        runtime: ToolRuntime[dict[str, object], Any],
        **arguments: object,
    ) -> str:
        """通过内部注册表执行工具并返回统一 JSON envelope。"""

        if runtime is None:
            return registry.execute(
                spec.name,
                arguments,
                ToolContext(),
            ).to_json()
        context = ToolContext.from_mapping(runtime.context)
        return registry.execute(spec.name, arguments, context).to_json()

    invoke.__name__ = spec.name
    invoke.__qualname__ = spec.name
    invoke.__doc__ = spec.description
    return StructuredTool.from_function(
        func=invoke,
        name=spec.name,
        description=spec.description,
        args_schema=runtime_args_model,
    )
