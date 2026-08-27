"""Agent 工具注册表的统一契约测试。"""

from langchain_core.messages import ToolMessage
from pydantic import BaseModel

from job_hunting_agent.agent import tool_message_to_output
from job_hunting_agent.intent_router import (
    DIRECT_TOOL_NAMES,
    DirectIntentExecutor,
    IntentDecision,
    build_intent_router_prompt,
)
from job_hunting_agent.job_hunting_tools import build_job_hunting_tool_registry
from job_hunting_agent.langchain_tool_adapter import build_langchain_tools
from job_hunting_agent.models import ImportedJob, MatchResult
from job_hunting_agent.tool_audit import tool_step_label
from job_hunting_agent.tool_registry import (
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from job_hunting_agent.web import (
    new_task_trace,
    reconcile_task_trace,
    task_trace_completion_status,
)


class EchoArgs(BaseModel):
    """测试工具参数。"""

    text: str


def test_tool_registry_validates_arguments_and_returns_one_result_envelope() -> None:
    """调用方只需理解一个执行接口和一种结果结构。"""

    registry = ToolRegistry(
        [
            ToolSpec(
                name="echo",
                description="返回输入文本。",
                args_model=EchoArgs,
                handler=lambda arguments, _context: ToolResult.success(
                    {"text": arguments.text}
                ),
                audit_label="回显文本",
                read_only=True,
                direct_route=True,
                direct_reply=lambda data: str(data["text"]),
            )
        ]
    )
    context = ToolContext(session_id="registry-test", root_request_id="request-1")

    succeeded = registry.execute("echo", {"text": "你好"}, context)
    rejected = registry.execute("echo", {}, context)

    assert succeeded.status == "success"
    assert succeeded.data == {"text": "你好"}
    assert succeeded.error is None
    assert succeeded.meta["tool_name"] == "echo"
    assert isinstance(succeeded.meta["elapsed_ms"], int)
    assert rejected.status == "rejected"
    assert rejected.error is not None
    assert rejected.error.code == "invalid_arguments"


def test_job_hunting_tool_registry_is_the_single_source_of_tool_metadata() -> None:
    """Agent、路由和审计所需元数据都能从一个注册表发现。"""

    registry = build_job_hunting_tool_registry(object())  # type: ignore[arg-type]

    assert [spec.name for spec in registry.list_specs()] == [
        "ingest_candidate_message",
        "get_current_candidate_profile",
        "list_candidate_profiles",
        "search_candidate_evidence",
        "import_job_from_text",
        "list_imported_jobs",
        "match_all_jobs_for_candidate",
        "list_project_cards_for_candidate",
        "analyze_github_project_for_candidate",
        "confirm_project_card",
        "create_resume_draft_for_job",
        "list_resume_artifacts_for_candidate",
        "create_tailored_resume_from_upload",
    ]
    assert {spec.name for spec in registry.direct_specs()} == {
        "get_current_candidate_profile",
        "list_candidate_profiles",
        "search_candidate_evidence",
        "list_imported_jobs",
        "match_all_jobs_for_candidate",
        "list_project_cards_for_candidate",
    }
    assert registry.get("confirm_project_card").requires_confirmation is True
    assert (
        registry.get("analyze_github_project_for_candidate").execution_mode
        == "background"
    )
    assert all(spec.audit_label for spec in registry.list_specs())
    assert all(spec.input_schema()["type"] == "object" for spec in registry.list_specs())


def test_langchain_adapter_exposes_registry_names_descriptions_and_schemas() -> None:
    """LangChain adapter 不再维护第二份工具定义。"""

    registry = build_job_hunting_tool_registry(object())  # type: ignore[arg-type]
    tools = build_langchain_tools(registry)

    assert [tool.name for tool in tools] == [spec.name for spec in registry.list_specs()]
    assert [tool.description for tool in tools] == [
        spec.description for spec in registry.list_specs()
    ]
    visible_schemas = [tool.tool_call_schema.model_json_schema() for tool in tools]
    expected_schemas = [spec.input_schema() for spec in registry.list_specs()]
    assert [schema.get("properties") for schema in visible_schemas] == [
        schema.get("properties") for schema in expected_schemas
    ]
    assert [schema.get("required", []) for schema in visible_schemas] == [
        schema.get("required", []) for schema in expected_schemas
    ]


def test_direct_route_executes_the_same_registered_match_tool() -> None:
    """直达路径必须返回注册表中的完整职位和匹配结果，不能另写一套实现。"""

    class MatchApp:
        def list_jobs(self, *, account_id=None):
            return [
                ImportedJob(
                    id=7,
                    raw_text="Python 后端开发",
                    source_url=None,
                    title="Python 后端开发",
                    city="杭州",
                    salary_min_k=15,
                    salary_max_k=20,
                    salary_months=12,
                    salary_unit="K/月",
                    experience_min_years=1,
                    experience_max_years=3,
                    experience_label="1-3年",
                    education="本科",
                    company_name="示例公司",
                    industry=None,
                    company_size=None,
                    skills=["Python"],
                    description_text="负责后端开发",
                    field_confidence={},
                    uncertainty_notes=[],
                )
            ]

        def match_all_jobs(self, candidate_id, *, account_id=None):
            return [
                MatchResult(
                    job_id=7,
                    candidate_id=candidate_id,
                    score=88,
                    tier="强推荐",
                    eliminated=False,
                    reasons=["技能匹配"],
                    elimination_reasons=[],
                    deductions=[],
                    risks=[],
                    uncertainty_notes=[],
                    resume_suggestions=[],
                )
            ]

    registry = build_job_hunting_tool_registry(MatchApp())  # type: ignore[arg-type]
    context = ToolContext(
        candidate_id=3,
        account_id=2,
        session_id="direct-match",
        root_request_id="request-match",
        use_tool_llm=False,
    )
    canonical = registry.execute("match_all_jobs_for_candidate", {}, context)
    reply, outputs = DirectIntentExecutor(registry).execute(
        IntentDecision(
            route="direct_tool",
            tool_name="match_all_jobs_for_candidate",
            confidence=1,
        ),
        candidate_id=3,
        account_id=2,
        session_id="direct-match",
        root_request_id="request-match",
    )

    assert reply == "已完成职位匹配，共分析 1 个职位。"
    assert outputs[0]["data"] == canonical.data
    assert outputs[0]["data"]["matches"][0]["job"]["title"] == "Python 后端开发"


def test_langchain_tool_envelope_is_flattened_for_existing_trace_consumers() -> None:
    """统一 envelope 不改变 Web 和审计读取 tool_outputs 的方式。"""

    result = ToolResult.rejected("invalid_job_text", "无法识别职位原文。")
    result.meta = {"tool_name": "import_job_from_text", "elapsed_ms": 4}

    output = tool_message_to_output(
        ToolMessage(
            content=result.to_json(),
            tool_call_id="call-invalid-job",
            name="import_job_from_text",
        )
    )

    assert output["status"] == "rejected"
    assert output["data"] is None
    assert output["error"] == {
        "code": "invalid_job_text",
        "message": "无法识别职位原文。",
        "retryable": False,
    }
    assert output["meta"]["elapsed_ms"] == 4


def test_router_and_audit_metadata_are_generated_from_the_tool_catalog() -> None:
    """路由提示词、白名单和审计标题不能再各自维护一份工具清单。"""

    registry = build_job_hunting_tool_registry(object())  # type: ignore[arg-type]
    direct_specs = registry.direct_specs()
    prompt = build_intent_router_prompt("列出职位", candidate_id=1)

    assert DIRECT_TOOL_NAMES == frozenset(spec.name for spec in direct_specs)
    assert all(spec.name in prompt for spec in direct_specs)
    assert "create_tailored_resume_from_upload" not in prompt
    assert all(
        tool_step_label(spec.name) == spec.audit_label
        for spec in registry.list_specs()
    )


def test_rejected_tool_envelope_marks_the_web_trace_as_failed() -> None:
    """统一错误字段必须贯通到管理端轨迹，不能被记录为已完成。"""

    trace = new_task_trace("request-rejected-tool")
    reconcile_task_trace(
        trace,
        [
            {
                "tool_name": "import_job_from_text",
                "status": "rejected",
                "data": None,
                "error": {
                    "code": "invalid_job_text",
                    "message": "不像一段完整的招聘职位信息。",
                    "retryable": False,
                },
            }
        ],
    )

    step = trace["steps"][0]
    assert step["status"] == "failed"
    assert step["result"] == {
        "ok": False,
        "error": "不像一段完整的招聘职位信息。",
    }
    assert task_trace_completion_status(trace, None) == "failed"
