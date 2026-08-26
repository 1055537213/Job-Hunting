"""轻量意图路由的协议、白名单和 Agent 级联行为测试。"""

import asyncio
import threading
import time
from dataclasses import dataclass

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage

from job_hunting_agent.agent import JobHuntingAgent
from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.config import IntentRouterSettings, LLMSettings
from job_hunting_agent.intent_router import (
    IntentDecision,
    IntentRouter,
    agent_fallback_reason,
    parse_intent_decision,
    requires_agent_fallback,
)
from job_hunting_agent.models import CandidateProfileInput


class StaticLLMClient:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


class SlowAsyncLLMClient:
    """模拟可取消的慢供应商请求。"""

    def __init__(self, delay_seconds: float):
        self.delay_seconds = delay_seconds
        self.cancelled = False

    def complete(self, prompt: str) -> str:
        raise AssertionError("有异步接口时不应使用同步调用。")

    async def acomplete(self, prompt: str) -> str:
        try:
            await asyncio.sleep(self.delay_seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return (
            '{"route":"direct_tool","tool_name":"list_imported_jobs",'
            '"arguments":{},"confidence":1.0}'
        )


class SlowSyncLLMClient:
    """模拟无法取消但最终会结束的同步兼容客户端。"""

    def __init__(self, delay_seconds: float):
        self.delay_seconds = delay_seconds
        self.finished = threading.Event()

    def complete(self, prompt: str) -> str:
        try:
            time.sleep(self.delay_seconds)
            return (
                '{"route":"direct_tool","tool_name":"list_imported_jobs",'
                '"arguments":{},"confidence":1.0}'
            )
        finally:
            self.finished.set()


class ExplodingChatModel(FakeMessagesListChatModel):
    """如果直读路由错误地进入主 Agent，测试应立即失败。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise AssertionError("高置信度只读路由不应调用主 Agent 模型。")


@dataclass
class StubIntentRouter:
    decision: IntentDecision

    def route(
        self,
        message: str,
        *,
        history: list[BaseMessage],
        candidate_id: int | None,
        account_id: int | None,
        session_id: str,
        root_request_id: str,
    ) -> IntentDecision:
        return self.decision


def test_parse_intent_decision_accepts_only_high_confidence_read_route():
    decision = parse_intent_decision(
        '{"route":"direct_tool","tool_name":"list_imported_jobs",'
        '"arguments":{},"confidence":0.96}',
        confidence_threshold=0.9,
        candidate_id=1,
    )

    assert decision.route == "direct_tool"
    assert decision.tool_name == "list_imported_jobs"
    assert decision.confidence == 0.96


def test_parse_intent_decision_rejects_mutation_and_low_confidence_routes():
    mutation = parse_intent_decision(
        '{"route":"direct_tool","tool_name":"ingest_candidate_message",'
        '"arguments":{"message":"保存我的经历"},"confidence":0.99}',
        confidence_threshold=0.9,
        candidate_id=1,
    )
    low_confidence = parse_intent_decision(
        '{"route":"direct_tool","tool_name":"match_all_jobs_for_candidate",'
        '"arguments":{},"confidence":0.4}',
        confidence_threshold=0.9,
        candidate_id=1,
    )

    assert mutation.route == "agent"
    assert mutation.fallback_reason == "tool_not_allowed"
    assert low_confidence.route == "agent"
    assert low_confidence.fallback_reason == "low_confidence"


def test_intent_router_uses_short_history_and_returns_agent_on_invalid_json():
    client = StaticLLMClient("不是 JSON")
    settings = IntentRouterSettings(
        enabled=True,
        llm=LLMSettings(
            provider="test",
            model="small",
            api_key="test-key",
            base_url="https://example.test/v1",
        ),
        confidence_threshold=0.9,
        history_messages=1,
    )
    router = IntentRouter(object(), settings=settings, llm_client=client)  # type: ignore[arg-type]

    decision = router.route(
        "查找我的项目证据",
        history=[AIMessage(content="旧消息"), AIMessage(content="保留的最近消息")],
        candidate_id=1,
        account_id=2,
        session_id="session-1",
        root_request_id="request-1",
    )

    assert decision is not None
    assert decision.route == "agent"
    assert decision.model_attempted is True
    assert decision.decision_source == "model"
    assert decision.fallback_reason == "router_error"
    assert "保留的最近消息" in client.prompts[0]
    assert "旧消息" not in client.prompts[0]


@pytest.mark.parametrize(
    "message",
    [
        "继续刚才的操作",
        "这个也帮我做一下",
        "列出职位，然后帮我改简历",
        "把薪资改成 20K",
        "提高简历里的熟练度措辞",
        "确认这张项目卡片",
        "帮我导入这个职位",
        "根据 job_id 12 和 artifact_id 8 改写简历",
    ],
)
def test_risky_messages_deterministically_fall_back_to_agent(message):
    assert requires_agent_fallback(message) is True


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("继续刚才的操作", "ambiguous_reference"),
        ("列出职位，然后帮我改简历", "multi_step"),
        ("把薪资改成 20K", "mutation_or_confirmation"),
        ("   ", "empty_message"),
    ],
)
def test_deterministic_gate_reports_low_sensitive_reason(message, reason):
    assert agent_fallback_reason(message) == reason


@pytest.mark.parametrize(
    "message",
    [
        "查看我的候选人档案",
        "列出我已经导入的职位",
        "匹配当前职位池",
        "查看项目经历卡片",
        "查找我的 Python 项目证据",
    ],
)
def test_explicit_read_only_messages_can_reach_small_router(message):
    assert requires_agent_fallback(message) is False


def test_deterministic_gate_skips_small_model_call():
    client = StaticLLMClient(
        '{"route":"direct_tool","tool_name":"list_imported_jobs",'
        '"arguments":{},"confidence":1.0}'
    )
    settings = IntentRouterSettings(
        enabled=True,
        llm=LLMSettings(
            provider="test",
            model="small",
            api_key="test-key",
            base_url="https://example.test/v1",
        ),
    )
    router = IntentRouter(object(), settings=settings, llm_client=client)  # type: ignore[arg-type]

    decision = router.route(
        "这个也帮我做一下",
        history=[AIMessage(content="已完成职位匹配")],
        candidate_id=1,
        account_id=2,
        session_id="session-1",
        root_request_id="request-1",
    )

    assert decision is not None
    assert decision.route == "agent"
    assert client.prompts == []


def test_router_hard_timeout_cancels_async_request_and_falls_back():
    client = SlowAsyncLLMClient(delay_seconds=0.5)
    settings = IntentRouterSettings(
        enabled=True,
        llm=LLMSettings(
            provider="test",
            model="small",
            api_key="test-key",
            base_url="https://example.test/v1",
        ),
        hard_timeout_seconds=0.05,
    )
    router = IntentRouter(object(), settings=settings, llm_client=client)  # type: ignore[arg-type]

    started_at = time.monotonic()
    decision = router.route(
        "列出我已经导入的职位",
        candidate_id=1,
        account_id=2,
        session_id="session-timeout",
        root_request_id="request-timeout",
    )
    elapsed_seconds = time.monotonic() - started_at

    assert decision is not None
    assert decision.route == "agent"
    assert decision.fallback_reason == "router_timeout"
    assert decision.model_attempted is True
    assert decision.decision_source == "model"
    assert decision.latency_ms >= 40
    assert elapsed_seconds < 0.25
    assert client.cancelled is True


def test_router_hard_timeout_bounds_sync_compatibility_client():
    client = SlowSyncLLMClient(delay_seconds=0.15)
    settings = IntentRouterSettings(
        enabled=True,
        llm=LLMSettings(
            provider="test",
            model="small",
            api_key="test-key",
            base_url="https://example.test/v1",
        ),
        hard_timeout_seconds=0.03,
    )
    router = IntentRouter(object(), settings=settings, llm_client=client)  # type: ignore[arg-type]

    started_at = time.monotonic()
    decision = router.route(
        "列出我已经导入的职位",
        candidate_id=1,
        account_id=2,
        session_id="session-sync-timeout",
        root_request_id="request-sync-timeout",
    )
    elapsed_seconds = time.monotonic() - started_at

    assert decision is not None
    assert decision.fallback_reason == "router_timeout"
    assert elapsed_seconds < 0.12
    assert client.finished.wait(timeout=0.5) is True


def test_agent_direct_route_bypasses_main_model_for_read_only_query(account_id):
    app = JobHuntingApp()
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="待补充",
            education="本科",
            experience_years=1,
            skills={"Python": "项目使用"},
            preferred_cities=["杭州"],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=["后端开发"],
            unacceptable=[],
        ),
        account_id=account_id,
    )
    agent = JobHuntingAgent(
        app,
        model=ExplodingChatModel(responses=[AIMessage(content="不应调用")]),
        intent_router=StubIntentRouter(
            IntentDecision(
                route="direct_tool",
                tool_name="get_current_candidate_profile",
                confidence=0.99,
                model_attempted=True,
                decision_source="model",
                latency_ms=12,
            )
        ),
    )

    result = agent.chat(
        "查看我的档案",
        candidate_id=candidate_id,
        session_id="direct-route",
        use_tool_llm=False,
        account_id=account_id,
    )

    assert result.mode == "intent_router_direct"
    assert result.used_tools == ["get_current_candidate_profile"]
    assert result.tool_outputs[0]["data"]["name"] == "小林"
    assert result.routing["selected_route"] == "direct_tool"
    assert result.routing["direct_executed"] is True
    assert result.routing["main_agent_used"] is False
    assert result.routing["router_latency_ms"] == 12
    assert {"message", "prompt", "raw_response"}.isdisjoint(result.routing)
