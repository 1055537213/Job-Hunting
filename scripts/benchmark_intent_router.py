"""对真实轻量意图路由模型运行一组低成本回归样本。"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage  # noqa: E402

from job_hunting_agent.intent_router import IntentRouter  # noqa: E402
from job_hunting_agent.model_gateway import ModelGateway  # noqa: E402


@dataclass(frozen=True)
class BenchmarkCase:
    """一个不依赖真实业务数据的路由判断样本。"""

    name: str
    message: str
    expected_route: str
    expected_tool: str | None = None
    candidate_id: int | None = 1
    history: tuple[BaseMessage, ...] = field(default_factory=tuple)


CASES = (
    BenchmarkCase("current_profile", "查看我的候选人档案", "direct_tool", "get_current_candidate_profile"),
    BenchmarkCase("all_profiles", "列出所有候选人档案", "direct_tool", "list_candidate_profiles"),
    BenchmarkCase("imported_jobs", "列出我已经导入的职位", "direct_tool", "list_imported_jobs"),
    BenchmarkCase(
        "match_jobs",
        "匹配当前候选人与职位池",
        "direct_tool",
        "match_all_jobs_for_candidate",
    ),
    BenchmarkCase(
        "project_cards",
        "列出我的项目经历卡片",
        "direct_tool",
        "list_project_cards_for_candidate",
    ),
    BenchmarkCase(
        "candidate_evidence",
        "检索我的 Python 项目证据",
        "direct_tool",
        "search_candidate_evidence",
    ),
    BenchmarkCase(
        "context_followup",
        "继续刚才的操作",
        "agent",
        history=(
            HumanMessage(content="请匹配我的职位"),
            AIMessage(content="已经完成职位匹配。"),
        ),
    ),
    BenchmarkCase(
        "context_pronoun",
        "这个也帮我做一下",
        "agent",
        history=(AIMessage(content="刚才已生成一份职位定制简历。"),),
    ),
    BenchmarkCase("multi_intent", "列出职位，然后帮我改简历", "agent"),
    BenchmarkCase("profile_mutation", "那薪资改成 20K", "agent"),
    BenchmarkCase("resume_truthfulness", "把简历里的 Python 改成精通", "agent"),
    BenchmarkCase("missing_candidate", "查看我的候选人档案", "agent", candidate_id=None),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--json", action="store_true", help="只输出 JSON 结果。")
    parser.add_argument(
        "--min-accuracy",
        type=float,
        default=0.0,
        help="准确率低于此值时返回非零状态，默认只测量不设门禁。",
    )
    return parser.parse_args()


def run_benchmark(env_file: Path) -> dict[str, object]:
    gateway = ModelGateway(env_path=env_file)
    router = IntentRouter(gateway)
    if not router.settings.enabled:
        raise ValueError(f"轻量意图路由未启用，请检查 {env_file}。")

    results: list[dict[str, object]] = []
    for case in CASES:
        started_at = time.monotonic()
        decision = router.route(
            case.message,
            history=case.history,
            candidate_id=case.candidate_id,
            account_id=None,
            session_id="intent-router-benchmark",
            root_request_id=uuid.uuid4().hex,
        )
        elapsed = max(0, round((time.monotonic() - started_at) * 1000))
        if decision is None:
            actual_route = "agent"
            actual_tool = None
            result = {
                "name": case.name,
                "expected_route": case.expected_route,
                "expected_tool": case.expected_tool,
                "passed": False,
                "actual_route": actual_route,
                "actual_tool": actual_tool,
                "decision_source": "disabled",
                "fallback_reason": "router_disabled",
                "confidence": 0.0,
                "model_attempted": False,
                "router_latency_ms": 0,
                "elapsed_ms": elapsed,
            }
        else:
            actual_route = decision.route
            actual_tool = decision.tool_name
            passed = actual_route == case.expected_route and (
                case.expected_tool is None or actual_tool == case.expected_tool
            )
            result = {
                "name": case.name,
                "expected_route": case.expected_route,
                "expected_tool": case.expected_tool,
                "passed": passed,
                "actual_route": actual_route,
                "actual_tool": actual_tool,
                "decision_source": decision.decision_source,
                "fallback_reason": decision.fallback_reason,
                "confidence": round(decision.confidence, 4),
                "model_attempted": decision.model_attempted,
                "router_latency_ms": decision.latency_ms,
                "elapsed_ms": elapsed,
            }
        results.append(result)

    passed_count = sum(bool(item["passed"]) for item in results)
    expected_direct = [item for item in results if item["expected_route"] == "direct_tool"]
    direct_hits = sum(bool(item["passed"]) for item in expected_direct)
    safe_fallbacks = sum(
        item["expected_route"] == "direct_tool" and item["actual_route"] == "agent"
        for item in results
    )
    unsafe_misroutes = sum(
        item["actual_route"] == "direct_tool" and not item["passed"] for item in results
    )
    model_latencies = [
        int(item["router_latency_ms"])
        for item in results
        if item["model_attempted"]
    ]
    fallback_reasons = Counter(
        str(item["fallback_reason"])
        for item in results
        if item["fallback_reason"] is not None
    )
    return {
        "summary": {
            "cases": len(results),
            "passed": passed_count,
            "accuracy": round(passed_count / len(results), 4),
            "direct_hit_rate": round(direct_hits / len(expected_direct), 4),
            "safe_fallbacks": safe_fallbacks,
            "unsafe_misroutes": unsafe_misroutes,
            "model_attempts": len(model_latencies),
            "guarded_without_model": sum(
                item["decision_source"] == "guard" for item in results
            ),
            "average_model_latency_ms": (
                round(statistics.mean(model_latencies)) if model_latencies else 0
            ),
            "p95_model_latency_ms": percentile_95(model_latencies),
            "fallback_reasons": dict(sorted(fallback_reasons.items())),
        },
        "cases": results,
    }


def percentile_95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(0.95 * (len(ordered) - 1))))
    return ordered[index]


def print_human_report(report: dict[str, object]) -> None:
    summary = report["summary"]
    assert isinstance(summary, dict)
    print(
        "Intent router benchmark: "
        f"{summary['passed']}/{summary['cases']} passed, "
        f"accuracy={summary['accuracy']}, "
        f"direct_hit_rate={summary['direct_hit_rate']}, "
        f"safe_fallbacks={summary['safe_fallbacks']}, "
        f"unsafe_misroutes={summary['unsafe_misroutes']}, "
        f"model_attempts={summary['model_attempts']}, "
        f"guarded_without_model={summary['guarded_without_model']}, "
        f"avg_model_latency={summary['average_model_latency_ms']}ms, "
        f"p95={summary['p95_model_latency_ms']}ms"
    )
    cases = report["cases"]
    assert isinstance(cases, list)
    for item in cases:
        assert isinstance(item, dict)
        status = "PASS" if item["passed"] else "FAIL"
        print(
            f"[{status}] {item['name']}: {item['actual_route']} "
            f"tool={item['actual_tool']} source={item['decision_source']} "
            f"reason={item['fallback_reason']} elapsed={item['elapsed_ms']}ms"
        )


def main() -> int:
    args = parse_args()
    if not 0.0 <= args.min_accuracy <= 1.0:
        raise ValueError("--min-accuracy 必须在 0 到 1 之间。")
    report = run_benchmark(args.env_file)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human_report(report)
    summary = report["summary"]
    assert isinstance(summary, dict)
    return 0 if float(summary["accuracy"]) >= args.min_accuracy else 1


if __name__ == "__main__":
    raise SystemExit(main())
