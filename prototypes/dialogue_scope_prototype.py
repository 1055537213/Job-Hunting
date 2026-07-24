"""
PROTOTYPE - throwaway #8 dialogue narrowing flow.

Run with:
    python ./prototypes/dialogue_scope_prototype.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pprint import pprint


@dataclass
class CandidateProfile:
    """用于原型的候选人档案摘要。

    正式版本会从 SQLite 读取候选人档案；这里用固定数据方便观察流程。
    """

    education: str = "本科"
    experience_years: float = 1.0
    skills: list[str] = field(
        default_factory=lambda: ["Python", "LangChain", "FastAPI", "SQLite", "向量检索"]
    )
    inferred_directions: list[str] = field(
        default_factory=lambda: ["AI Agent 应用开发", "Python 后端开发", "RAG 应用开发"]
    )


@dataclass
class SearchScope:
    """一次职位发现对话中逐步收窄出来的搜索范围。"""

    goal: str | None = None
    directions: list[str] = field(default_factory=list)
    include_adjacent: bool | None = None
    cities: list[str] = field(default_factory=list)
    remote_ok: bool | None = None
    work_modes: list[str] = field(default_factory=list)
    salary_floor_k: int | None = None
    salary_expected_k: int | None = None
    unacceptable: list[str] = field(default_factory=list)
    company_preferences: list[str] = field(default_factory=list)
    boss_filters: dict[str, object] = field(default_factory=dict)
    search_batches: list[dict[str, object]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def ask(prompt: str, default: str = "") -> str:
    """读取用户输入；如果直接回车，就采用默认值。"""

    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"\n{prompt}{suffix}\n> ").strip()
    except EOFError:
        value = ""
    return value or default


def split_csv(value: str) -> list[str]:
    """把中英文逗号分隔的输入转成列表。"""

    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]


def show_state(step: str, scope: SearchScope) -> None:
    """每一步都打印完整状态，方便观察对话如何收窄范围。"""

    print(f"\n--- 当前筛选状态：{step} ---")
    pprint(scope.__dict__, sort_dicts=False)


def build_boss_filters(profile: CandidateProfile, scope: SearchScope) -> None:
    """把已确认搜索范围转成 BOSS 平台筛选建议。

    原型只生成建议，不访问 BOSS，也不自动抓取页面。
    """

    scope.boss_filters = {
        "query_keywords": scope.directions,
        "city": scope.cities,
        "salary_floor_k": scope.salary_floor_k,
        "experience_strategy": {
            "candidate_years": profile.experience_years,
            "exclude_if_gap_years_gte": 3,
            "recommended_filter": "优先选择低于或接近候选人经验的岗位，保留少量冲刺岗位",
        },
        "education_strategy": {
            "candidate_education": profile.education,
            "must_meet_job_requirement": True,
        },
        "work_modes": scope.work_modes,
        "remote_ok": scope.remote_ok,
        "unacceptable": scope.unacceptable,
        "company_preferences": scope.company_preferences,
    }


def build_search_batches(scope: SearchScope) -> None:
    """按城市和岗位方向拆分搜索批次。"""

    scope.search_batches = []
    for city in scope.cities or ["全国"]:
        for direction in scope.directions or ["未确认方向"]:
            scope.search_batches.append(
                {
                    "boss_query": direction,
                    "city": city,
                    "salary_floor_k": scope.salary_floor_k,
                    "why": "先用候选人确认的方向和城市生成官方搜索入口，再由候选人复制可见职位信息导入。",
                }
            )


def main() -> None:
    """运行 #8 对话式缩小范围原型。"""

    profile = CandidateProfile()
    scope = SearchScope()

    print("PROTOTYPE #8：对话式缩小范围")
    print("候选人档案摘要：")
    pprint(profile.__dict__, sort_dicts=False)

    scope.goal = ask(
        "这次搜索优先目标是什么？1=尽快找到可投岗位，2=精准匹配目标方向，3=冲刺更高薪资",
        "2",
    )
    show_state("1. 搜索目标", scope)

    directions = ask(
        "确认岗位方向，可多选逗号分隔。推荐：AI Agent 应用开发, Python 后端开发, RAG 应用开发",
        ", ".join(profile.inferred_directions[:2]),
    )
    scope.directions = split_csv(directions)
    show_state("2. 岗位方向", scope)

    adjacent = ask("是否允许包含相邻岗位？y=允许，n=只看精确方向", "y")
    scope.include_adjacent = adjacent.lower().startswith("y")
    if scope.include_adjacent:
        scope.notes.append("相邻岗位进入排序扣分，不作为硬性淘汰。")
    show_state("3. 搜索宽度", scope)

    cities = ask("可接受城市，可多选逗号分隔", "杭州, 上海")
    scope.cities = split_csv(cities)
    show_state("4. 城市", scope)

    remote = ask("是否接受远程或混合办公？y=接受，n=不接受", "y")
    scope.remote_ok = remote.lower().startswith("y")
    show_state("5. 远程/混合办公", scope)

    modes = ask("可接受工作形式，可多选逗号分隔", "全职")
    scope.work_modes = split_csv(modes)
    show_state("6. 工作形式", scope)

    salary_floor = ask("薪资硬底线是多少 K/月？职位薪资上限低于该底线时会淘汰", "10")
    expected_salary = ask("期望薪资是多少 K/月？用于排序，不硬淘汰", "15")
    scope.salary_floor_k = int(salary_floor)
    scope.salary_expected_k = int(expected_salary)
    show_state("7. 薪资", scope)

    unacceptable = ask(
        "明确不能接受的条件，可多选逗号分隔，例如 外包, 长期出差, 倒班",
        "外包, 长期出差",
    )
    scope.unacceptable = split_csv(unacceptable)
    show_state("8. 硬性不可接受条件", scope)

    company_preferences = ask(
        "公司偏好，可多选逗号分隔。例如 AI 行业, 100人以上, 已融资。偏好只影响排序",
        "AI 行业, 100人以上",
    )
    scope.company_preferences = split_csv(company_preferences)
    show_state("9. 公司偏好", scope)

    build_boss_filters(profile, scope)
    build_search_batches(scope)
    show_state("10. 生成 BOSS 搜索方案", scope)

    print("\n原型结论：")
    print("先问目标和岗位方向，再问城市/工作形式这些平台筛选项，随后确认薪资硬底线、不可接受条件和公司偏好。")
    print("经验和学历来自候选人档案，不反复问；只在生成筛选策略和匹配规则时展示给候选人确认。")


if __name__ == "__main__":
    main()
