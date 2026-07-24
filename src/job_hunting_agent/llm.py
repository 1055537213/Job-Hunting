"""LLM 适配器边界。

当前 MVP 不绑定任何具体大模型供应商。业务代码只依赖 `LLMClient`
这个极小接口：输入 prompt，返回文本。以后无论接 OpenAI、DeepSeek、
本地模型还是 LangChain 封装，都应该在这个模块里新增适配器，
不要让业务流程直接依赖某个厂商 SDK。
"""

from __future__ import annotations

from typing import Protocol


class LLMClient(Protocol):
    """业务层需要的最小 LLM 能力。

    这里故意不暴露 temperature、model、messages 等供应商细节；这些配置应由
    具体适配器内部处理。这样 `resume_writer.py` 可以专注真实性边界。
    """

    def complete(self, prompt: str) -> str:
        """根据 prompt 生成文本。"""


class StaticLLMClient:
    """测试或演示用的静态 LLM。

    它不会联网，也不需要 API Key。真实供应商适配器接入前，可以用它验证
    “LLM 输出进入业务流程后会被安全检查”这件事。
    """

    def __init__(self, response: str):
        """保存固定响应文本。"""

        self.response = response

    def complete(self, prompt: str) -> str:
        """忽略 prompt，直接返回固定文本。"""

        return self.response
