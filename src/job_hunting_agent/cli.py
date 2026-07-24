"""命令行入口。

CLI 只负责把用户输入转成应用服务调用，并把结果打印成 JSON。
真正的业务逻辑不要写在这里，应该放在 `JobHuntingApp` 或更底层模块中。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .app import JobHuntingApp
from .models import CandidateProfileInput


def main(argv: list[str] | None = None) -> None:
    """解析命令行参数并分发到对应功能。"""

    parser = argparse.ArgumentParser(prog="job-agent")
    # 所有命令共用同一个本地 SQLite 数据库；默认放在 data/ 下，已被 .gitignore 忽略。
    parser.add_argument("--db", default="data/job_agent.db")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init")

    create_parser = subparsers.add_parser("create-profile")
    create_parser.add_argument("--from-json")
    create_parser.add_argument("--name")
    create_parser.add_argument("--status")
    create_parser.add_argument("--education")
    create_parser.add_argument("--experience-years", type=float)
    create_parser.add_argument("--skills")
    create_parser.add_argument("--cities")
    create_parser.add_argument("--salary-floor-k", type=int)
    create_parser.add_argument("--expected-salary-k", type=int)
    create_parser.add_argument("--directions")
    create_parser.add_argument("--unacceptable")

    demo_parser = subparsers.add_parser("demo")
    demo_parser.add_argument("--project", default=".")

    analyze_parser = subparsers.add_parser("analyze-project")
    analyze_parser.add_argument("project_path")
    analyze_parser.add_argument("--candidate-id", type=int)

    confirm_parser = subparsers.add_parser("confirm-project")
    confirm_parser.add_argument("record_id", type=int)
    confirm_summary = confirm_parser.add_mutually_exclusive_group()
    confirm_summary.add_argument("--summary")
    confirm_summary.add_argument("--summary-file")

    list_projects_parser = subparsers.add_parser("list-projects")
    list_projects_parser.add_argument("candidate_id", type=int)

    import_parser = subparsers.add_parser("import-job")
    import_parser.add_argument("text_file")
    import_parser.add_argument("--source-url")

    import_many_parser = subparsers.add_parser("import-jobs")
    import_many_parser.add_argument("text_file")
    import_many_parser.add_argument("--source-url")
    # 多职位文本导入采用显式分隔符，避免系统擅自猜测 BOSS 页面文本边界。
    import_many_parser.add_argument("--separator", default="---JOB---")

    subparsers.add_parser("list-jobs")

    match_parser = subparsers.add_parser("match")
    match_parser.add_argument("candidate_id", type=int)
    match_parser.add_argument("job_id", type=int)

    match_all_parser = subparsers.add_parser("match-all")
    match_all_parser.add_argument("candidate_id", type=int)

    args = parser.parse_args(argv)
    app = JobHuntingApp(args.db)
    # 每次运行命令前都初始化表结构，让新手不用单独记住建表步骤。
    app.initialize()

    if args.command == "init":
        print_json({"status": "ok", "db": str(Path(args.db).resolve())})
    elif args.command == "create-profile":
        profile = build_profile_from_cli(args)
        candidate_id = app.save_candidate_profile(profile)
        print_json({"candidate_id": candidate_id, "profile": asdict(app.get_candidate_profile(candidate_id))})
    elif args.command == "demo":
        run_demo(app, args.project)
    elif args.command == "analyze-project":
        if args.candidate_id is None:
            print_json(asdict(app.analyze_project(args.project_path)))
        else:
            print_json(asdict(app.analyze_project_for_candidate(args.candidate_id, args.project_path)))
    elif args.command == "confirm-project":
        print_json(asdict(app.confirm_project_card(args.record_id, read_confirmation_summary(args))))
    elif args.command == "list-projects":
        print_json(
            {
                "candidate_id": args.candidate_id,
                "project_cards": [asdict(record) for record in app.list_project_cards(args.candidate_id)],
            }
        )
    elif args.command == "import-job":
        raw_text = Path(args.text_file).read_text(encoding="utf-8")
        print_json(asdict(app.import_job_text(raw_text, args.source_url)))
    elif args.command == "import-jobs":
        raw_text = Path(args.text_file).read_text(encoding="utf-8")
        jobs = [app.import_job_text(text, args.source_url) for text in split_job_texts(raw_text, args.separator)]
        print_json({"count": len(jobs), "jobs": [asdict(job) for job in jobs]})
    elif args.command == "list-jobs":
        print_json({"jobs": [asdict(job) for job in app.list_jobs()]})
    elif args.command == "match":
        print_json(asdict(app.match_job(args.candidate_id, args.job_id)))
    elif args.command == "match-all":
        matches = app.match_all_jobs(args.candidate_id)
        jobs_by_id = {job.id: job for job in app.list_jobs()}
        print_json(
            {
                "candidate_id": args.candidate_id,
                "matches": [
                    {"job": asdict(jobs_by_id[match.job_id]), "match": asdict(match)}
                    for match in matches
                ],
            }
        )


def build_profile_from_cli(args: argparse.Namespace) -> CandidateProfileInput:
    """根据 CLI 参数、JSON 文件或交互式问答创建候选人档案输入。"""

    data = load_profile_json(args.from_json) if args.from_json else {}
    # 命令行参数优先于 JSON；两者都没有时进入交互式提问，适合新手逐步填写。
    return CandidateProfileInput(
        name=args.name or data.get("name") or prompt_required("姓名"),
        status=args.status or data.get("status") or prompt_required("当前状态，例如 在职/离职/应届"),
        education=args.education or data.get("education") or prompt_required("最高学历，例如 大专/本科/硕士"),
        experience_years=first_present(
            args.experience_years,
            data.get("experience_years"),
            lambda: float(prompt_required("实际工作经验年限，例如 1 或 1.5")),
        ),
        skills=first_present(
            parse_skills(args.skills),
            data.get("skills"),
            lambda: parse_skills(prompt_optional("技能，格式 Python=项目使用,FastAPI=了解")),
        )
        or {},
        preferred_cities=first_present(
            split_items(args.cities),
            data.get("preferred_cities"),
            lambda: split_items(prompt_optional("可接受城市，逗号分隔，例如 杭州,上海")),
        )
        or [],
        salary_floor_k=first_present(
            args.salary_floor_k,
            data.get("salary_floor_k"),
            lambda: parse_int_or_none(prompt_optional("薪资硬底线，单位 K，可留空")),
        ),
        expected_salary_k=first_present(
            args.expected_salary_k,
            data.get("expected_salary_k"),
            lambda: parse_int_or_none(prompt_optional("期望薪资，单位 K，可留空")),
        ),
        target_directions=first_present(
            split_items(args.directions),
            data.get("target_directions"),
            lambda: split_items(prompt_optional("目标方向，逗号分隔，例如 AI Agent 应用开发,Python 后端")),
        )
        or [],
        unacceptable=first_present(
            split_items(args.unacceptable),
            data.get("unacceptable"),
            lambda: split_items(prompt_optional("明确不可接受条件，逗号分隔，例如 外包,长期出差")),
        )
        or [],
    )


def load_profile_json(path: str) -> dict[str, object]:
    """读取候选人档案 JSON 文件。"""

    return json.loads(Path(path).read_text(encoding="utf-8"))


def first_present(*values_or_factories: object) -> object:
    """返回第一个不是 None 的值；可接收无参函数作为延迟兜底。

    这个小工具让“参数 -> JSON -> 交互式输入”的优先级更清晰。
    """

    for value in values_or_factories:
        resolved = value() if callable(value) else value
        if resolved is not None:
            return resolved
    return None


def prompt_required(label: str) -> str:
    """持续询问一个必填字段，直到用户输入非空文本。"""

    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print("这个字段不能为空，请再输入一次。")


def prompt_optional(label: str) -> str:
    """询问一个可选字段，允许用户直接回车跳过。"""

    return input(f"{label}: ").strip()


def split_items(value: str | list[str] | None) -> list[str] | None:
    """把逗号/中文逗号分隔的文本转成列表。"""

    if value is None:
        return None
    if isinstance(value, list):
        return value
    normalized = value.replace("，", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def parse_skills(value: str | dict[str, str] | None) -> dict[str, str] | None:
    """解析技能输入。

    推荐格式是 `Python=项目使用,FastAPI=了解`；如果只写技能名，则熟练度先标为
    `待确认`，后续再由候选人补充。
    """

    if value is None:
        return None
    if isinstance(value, dict):
        return value
    skills: dict[str, str] = {}
    for item in split_items(value) or []:
        if "=" in item:
            skill, level = item.split("=", 1)
        elif ":" in item:
            skill, level = item.split(":", 1)
        else:
            skill, level = item, "待确认"
        if skill.strip():
            skills[skill.strip()] = level.strip() or "待确认"
    return skills


def parse_int_or_none(value: str | int | None) -> int | None:
    """把可选整数输入转成 `int | None`。"""

    if value in (None, ""):
        return None
    return int(value)


def read_confirmation_summary(args: argparse.Namespace) -> str | None:
    """读取项目卡片确认摘要，支持命令行文本或文件。"""

    if args.summary_file:
        return Path(args.summary_file).read_text(encoding="utf-8").strip()
    if args.summary:
        return args.summary.strip()
    return None


def split_job_texts(raw_text: str, separator: str) -> list[str]:
    """按显式分隔符拆分批量职位文本。

    BOSS 页面复制内容边界不稳定，所以 MVP 不自动猜测多个职位在哪里切开；
    使用者可以在不同职位之间手动放入 `---JOB---`。
    """

    chunks = [chunk.strip() for chunk in raw_text.split(separator)]
    return [chunk for chunk in chunks if chunk]


def run_demo(app: JobHuntingApp, project_path: str) -> None:
    """跑通第一条端到端演示链路。

    演示链路包含：创建候选人档案、分析项目目录、导入职位文本、输出匹配解释。
    它不是正式产品流程，只是让用户一条命令看见 MVP 能力。
    """

    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="演示候选人",
            status="离职",
            education="本科",
            experience_years=1.0,
            skills={
                "Python": "项目使用",
                "LangChain": "项目使用",
                "FastAPI": "项目使用",
                "SQLite": "项目使用",
                "向量检索": "项目使用",
            },
            preferred_cities=["杭州", "上海"],
            salary_floor_k=10,
            expected_salary_k=15,
            target_directions=["AI Agent 应用开发", "Python 后端开发"],
            unacceptable=["外包", "长期出差"],
        )
    )
    project_card = app.analyze_project(project_path)
    job = app.import_job_text(
        # 这里使用 BOSS 风格的职位文本样例；真实使用时由候选人从平台页面复制导入。
        """
        Python AI 应用开发工程师
        12-18K·14薪
        杭州
        1-3年
        本科
        星河智能
        人工智能
        20-99人
        职位描述：
        负责基于 Python、FastAPI 和 LangChain 的 Agent 应用开发，
        需要熟悉 SQLite、RAG、向量检索和职位文本处理。
        """
    )
    match = app.match_job(candidate_id, job.id)
    print_json(
        {
            "candidate_id": candidate_id,
            "project_card": asdict(project_card),
            "job": asdict(job),
            "match": asdict(match),
        }
    )


def print_json(value: object) -> None:
    """统一 JSON 输出，保留中文，方便人读也方便后续脚本处理。"""

    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
