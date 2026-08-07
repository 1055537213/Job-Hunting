"""命令行入口。

CLI 只负责把用户输入转成应用服务调用，并把结果打印成 JSON。
真正的业务逻辑不要写在这里，应该放在 `JobHuntingApp` 或更底层模块中。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .agent import JobHuntingAgent
from .app import JobHuntingApp
from .auth import hash_password
from .config import (
    load_database_settings,
    load_embedding_settings,
    load_llm_settings,
    load_rerank_settings,
    masked_database_settings,
    masked_embedding_settings,
    masked_llm_settings,
    masked_rerank_settings,
)
from .database_migrations import current_database_revision, upgrade_database
from .llm import LLMClient, StaticLLMClient
from .models import CandidateProfileInput


# PostgreSQL 是多账号事实源；以下命令会读取或写入账号归属数据，不能隐式使用全局范围。
ACCOUNT_SCOPED_COMMANDS = frozenset(
    {
        "create-profile",
        "ingest-message",
        "agent-chat",
        "demo",
        "confirm-project",
        "list-projects",
        "import-job",
        "import-jobs",
        "list-jobs",
        "rag-rebuild",
        "rag-search",
        "match",
        "match-all",
        "draft-resume",
        "list-resume-drafts",
    }
)


def main(argv: list[str] | None = None) -> None:
    """解析命令行参数并分发到对应功能。"""

    parser = argparse.ArgumentParser(prog="job-agent")
    # 未配置 PostgreSQL 时才使用 SQLite 离线兼容路径；默认文件已被 .gitignore 忽略。
    parser.add_argument("--db", default="data/job_agent.db")
    # 模型配置单独放在 .env，方便后续换供应商/模型，不需要改代码。
    parser.add_argument("--env-file", default=".env")
    # 仅 SQLite 离线兼容路径使用本地 Chroma；生产 PostgreSQL 会忽略这个参数。
    parser.add_argument("--rag-dir", default="data/chroma")
    # 生产 CLI 没有网页 Cookie，因此需要显式指定当前操作所属的账号。
    parser.add_argument("--account-id", type=int)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init")
    subparsers.add_parser("llm-config")
    subparsers.add_parser("embedding-config")
    subparsers.add_parser("rerank-config")
    subparsers.add_parser("database-config")
    subparsers.add_parser("database-current")
    subparsers.add_parser("database-upgrade")
    subparsers.add_parser("create-admin")

    web_parser = subparsers.add_parser("web")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", type=int, default=8000)

    create_parser = subparsers.add_parser("create-profile")
    create_parser.add_argument("--from-json")
    create_parser.add_argument("--name")
    create_parser.add_argument("--status")
    create_parser.add_argument("--education")
    create_parser.add_argument("--experience-years", type=float)
    create_parser.add_argument("--skills")
    create_parser.add_argument("--cities")
    create_parser.add_argument("--acceptable-cities")
    create_parser.add_argument("--salary-floor-k", type=int)
    create_parser.add_argument("--expected-salary-k", type=int)
    create_parser.add_argument("--directions")
    create_parser.add_argument("--unacceptable")

    ingest_parser = subparsers.add_parser("ingest-message")
    ingest_parser.add_argument("candidate_id", type=int)
    # 短资料可以直接作为命令参数传入；长资料建议放到文件里，避免命令行转义麻烦。
    ingest_parser.add_argument("message", nargs="?")
    ingest_parser.add_argument("--message-file")
    ingest_llm = ingest_parser.add_mutually_exclusive_group()
    ingest_llm.add_argument("--llm-static-response")
    ingest_llm.add_argument("--llm-static-response-file")
    ingest_llm.add_argument("--use-env-llm", action="store_true")
    ingest_parser.add_argument("--auto-rag", action="store_true")

    agent_chat_parser = subparsers.add_parser("agent-chat")
    agent_chat_parser.add_argument("candidate_id", type=int)
    # 标准 Agent 聊天入口同样支持短消息参数和长文本文件两种输入方式。
    agent_chat_parser.add_argument("message", nargs="?")
    agent_chat_parser.add_argument("--message-file")
    agent_chat_parser.add_argument("--session-id")

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

    subparsers.add_parser("rag-rebuild")

    rag_search_parser = subparsers.add_parser("rag-search")
    rag_search_parser.add_argument("query")
    rag_search_parser.add_argument("--top-k", type=int, default=5)
    rag_search_parser.add_argument("--entity-types")

    match_parser = subparsers.add_parser("match")
    match_parser.add_argument("candidate_id", type=int)
    match_parser.add_argument("job_id", type=int)

    match_all_parser = subparsers.add_parser("match-all")
    match_all_parser.add_argument("candidate_id", type=int)

    draft_parser = subparsers.add_parser("draft-resume")
    draft_parser.add_argument("candidate_id", type=int)
    draft_parser.add_argument("job_id", type=int)
    draft_llm = draft_parser.add_mutually_exclusive_group()
    # 静态响应入口用于教学演示“LLM 输出会被安全检查”；真实模型通过 .env 加载。
    draft_llm.add_argument("--llm-static-response")
    draft_llm.add_argument("--llm-static-response-file")
    draft_llm.add_argument("--use-env-llm", action="store_true")
    draft_parser.add_argument("--use-rag", action="store_true")
    draft_parser.add_argument("--rag-query")

    list_drafts_parser = subparsers.add_parser("list-resume-drafts")
    list_drafts_parser.add_argument("candidate_id", type=int)
    list_drafts_parser.add_argument("--job-id", type=int)

    args = parser.parse_args(argv)
    # 数据库迁移命令不能先构造 JobHuntingApp，否则会意外创建旧 SQLite 测试库。
    database_settings = load_database_settings(args.env_file)
    if args.command == "database-config":
        print_json(masked_database_settings(database_settings))
        return
    # 纯配置查询不应初始化应用，更不能因为查看配置而创建 SQLite 测试文件。
    if args.command == "llm-config":
        print_json(masked_llm_settings(load_llm_settings(args.env_file)))
        return
    if args.command == "embedding-config":
        print_json(masked_embedding_settings(load_embedding_settings(args.env_file)))
        return
    if args.command == "rerank-config":
        print_json(masked_rerank_settings(load_rerank_settings(args.env_file)))
        return
    if args.command in {"database-current", "database-upgrade"}:
        if not database_settings.configured or database_settings.url is None:
            raise SystemExit(
                "缺少 JOB_AGENT_DATABASE_URL；请在 .env 中配置 PostgreSQL 连接后再执行数据库命令。"
            )
        if args.command == "database-upgrade":
            revision = upgrade_database(database_settings.url)
            print_json(
                {
                    "status": "ok",
                    "database": database_settings.masked_url,
                    "revision": revision,
                }
            )
        else:
            print_json(
                {
                    "configured": True,
                    "database": database_settings.masked_url,
                    "revision": current_database_revision(database_settings.url),
                }
            )
        return

    # 配置了数据库 URL 时，CLI 与 Web 使用同一 PostgreSQL 仓储；未配置时仅保留
    # SQLite 测试/离线兼容路径，方便现有教学回归在临时文件中运行。
    app = JobHuntingApp(
        args.db,
        args.env_file,
        database_url=database_settings.url if database_settings.configured else None,
    )
    # 每次运行命令前都初始化表结构，让新手不用单独记住建表步骤。
    app.initialize()
    account_id = resolve_cli_account_id(app, args, production_database=database_settings.configured)

    if args.command == "init":
        print_json({"status": "ok", "db": str(Path(args.db).resolve())})
    elif args.command == "create-admin":
        create_admin_account(app)
    elif args.command == "web":
        run_web_server(args)
    elif args.command == "create-profile":
        profile = build_profile_from_cli(args)
        candidate_id = app.save_candidate_profile(profile, account_id=account_id)
        print_json(
            {
                "candidate_id": candidate_id,
                "profile": asdict(app.get_candidate_profile(candidate_id, account_id=account_id)),
            }
        )
    elif args.command == "ingest-message":
        print_json(
            asdict(
                app.ingest_conversation_message(
                    args.candidate_id,
                    read_ingestion_message(args),
                    llm_client=build_cli_llm(app, args, "tool_llm_ingestion", account_id=account_id),
                    rag_persist_directory=args.rag_dir,
                    auto_rebuild_rag=args.auto_rag,
                    account_id=account_id,
                )
            )
        )
    elif args.command == "agent-chat":
        message = read_ingestion_message(args)
        try:
            agent = JobHuntingAgent(app, env_path=args.env_file, rag_dir=args.rag_dir)
        except ValueError as error:
            # 如果 `.env` 没配好，CLI 仍然回退到本地规则入库，避免新入口完全不可用。
            fallback = app.ingest_conversation_message(
                args.candidate_id,
                message,
                llm_client=None,
                rag_persist_directory=args.rag_dir,
                auto_rebuild_rag=True,
                account_id=account_id,
            )
            save_cli_chat_turn(
                app,
                args.candidate_id,
                args.session_id or f"candidate-{args.candidate_id}",
                message,
                fallback.reply,
                {"mode": "rule_based_ingestion", "fallback_reason": str(error)},
                account_id=account_id,
            )
            print_json(
                {
                    "mode": "rule_based_ingestion",
                    "fallback_reason": str(error),
                    "result": asdict(fallback),
                }
            )
        else:
            result = agent.chat(
                message,
                candidate_id=args.candidate_id,
                session_id=args.session_id,
                use_tool_llm=True,
                auto_rag=True,
                account_id=account_id,
            )
            save_cli_chat_turn(
                app,
                args.candidate_id,
                result.session_id,
                message,
                result.reply,
                {"mode": result.mode, "used_tools": result.used_tools},
                account_id=account_id,
            )
            print_json(
                asdict(result)
            )
    elif args.command == "demo":
        run_demo(app, args.project, account_id=account_id)
    elif args.command == "analyze-project":
        if args.candidate_id is None:
            print_json(asdict(app.analyze_project(args.project_path)))
        else:
            print_json(
                asdict(
                    app.analyze_project_for_candidate(
                        args.candidate_id,
                        args.project_path,
                        account_id=account_id,
                    )
                )
            )
    elif args.command == "confirm-project":
        print_json(
            asdict(
                app.confirm_project_card(
                    args.record_id,
                    read_confirmation_summary(args),
                    account_id=account_id,
                )
            )
        )
    elif args.command == "list-projects":
        print_json(
            {
                "candidate_id": args.candidate_id,
                "project_cards": [
                    asdict(record)
                    for record in app.list_project_cards(args.candidate_id, account_id=account_id)
                ],
            }
        )
    elif args.command == "import-job":
        raw_text = Path(args.text_file).read_text(encoding="utf-8")
        print_json(asdict(app.import_job_text(raw_text, args.source_url, account_id=account_id)))
    elif args.command == "import-jobs":
        raw_text = Path(args.text_file).read_text(encoding="utf-8")
        jobs = [
            app.import_job_text(text, args.source_url, account_id=account_id)
            for text in split_job_texts(raw_text, args.separator)
        ]
        print_json({"count": len(jobs), "jobs": [asdict(job) for job in jobs]})
    elif args.command == "list-jobs":
        print_json({"jobs": [asdict(job) for job in app.list_jobs(account_id=account_id)]})
    elif args.command == "rag-rebuild":
        print_json(asdict(app.rebuild_rag_index(args.rag_dir, account_id=account_id)))
    elif args.command == "rag-search":
        results = app.search_rag(
            args.query,
            args.rag_dir,
            args.top_k,
            split_items(args.entity_types),
            account_id=account_id,
        )
        print_json({"query": args.query, "results": [asdict(result) for result in results]})
    elif args.command == "match":
        print_json(asdict(app.match_job(args.candidate_id, args.job_id, account_id=account_id)))
    elif args.command == "match-all":
        matches = app.match_all_jobs(args.candidate_id, account_id=account_id)
        jobs_by_id = {job.id: job for job in app.list_jobs(account_id=account_id)}
        print_json(
            {
                "candidate_id": args.candidate_id,
                "matches": [
                    {"job": asdict(jobs_by_id[match.job_id]), "match": asdict(match)}
                    for match in matches
                ],
            }
        )
    elif args.command == "draft-resume":
        print_json(
            asdict(
                app.create_resume_draft(
                    args.candidate_id,
                    args.job_id,
                    build_cli_llm(app, args, "resume_rewrite", account_id=account_id),
                    rag_persist_directory=args.rag_dir if args.use_rag else None,
                    rag_query=args.rag_query,
                    account_id=account_id,
                )
            )
        )
    elif args.command == "list-resume-drafts":
        print_json(
            {
                "candidate_id": args.candidate_id,
                "resume_drafts": [
                    asdict(record)
                    for record in app.list_resume_drafts(
                        args.candidate_id,
                        args.job_id,
                        account_id=account_id,
                    )
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
            lambda: split_items(prompt_optional("首选城市，逗号分隔，例如 杭州,上海")),
        )
        or [],
        acceptable_cities=first_present(
            split_items(args.acceptable_cities),
            data.get("acceptable_cities"),
            # 兼容旧版 JSON：文件未包含新字段时按空列表处理，不突然进入交互输入。
            [] if args.from_json else lambda: split_items(prompt_optional("其他可接受城市，逗号分隔，可留空")),
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


def save_cli_chat_turn(
    app: JobHuntingApp,
    candidate_id: int,
    session_id: str,
    user_message: str,
    assistant_message: str,
    assistant_metadata: dict[str, object],
    account_id: int | None = None,
) -> None:
    """保存一次 CLI Agent 聊天，让命令行和网页共用持久化对话记忆。"""

    app.save_chat_message(
        candidate_id,
        session_id,
        "user",
        user_message,
        {"source": "cli_agent_chat"},
        account_id=account_id,
    )
    app.save_chat_message(
        candidate_id,
        session_id,
        "assistant",
        assistant_message,
        assistant_metadata,
        account_id=account_id,
    )


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


def read_ingestion_message(args: argparse.Namespace) -> str:
    """读取对话式入库消息。

    CLI 同时支持短消息参数和长文本文件；两者只能选一种，避免把同一份资料重复入库。
    """

    if args.message_file and args.message:
        raise SystemExit("请只提供 MESSAGE 或 --message-file 其中一种。")
    if args.message_file:
        message = Path(args.message_file).read_text(encoding="utf-8").strip()
    else:
        message = (args.message or "").strip()
    if not message:
        raise SystemExit("请提供需要保存的资料消息，或使用 --message-file 指定文件。")
    return message


def split_job_texts(raw_text: str, separator: str) -> list[str]:
    """按显式分隔符拆分批量职位文本。

    BOSS 页面复制内容边界不稳定，所以 MVP 不自动猜测多个职位在哪里切开；
    使用者可以在不同职位之间手动放入 `---JOB---`。
    """

    chunks = [chunk.strip() for chunk in raw_text.split(separator)]
    return [chunk for chunk in chunks if chunk]


def build_cli_llm(
    app: JobHuntingApp,
    args: argparse.Namespace,
    operation: str,
    account_id: int | None = None,
) -> LLMClient | None:
    """根据 CLI 参数构造 LLM 客户端。

    默认返回 None，表示使用本地规则逻辑；传入 `--use-env-llm` 时才会读取
    `.env` 调用真实模型，避免用户不小心产生成本或网络请求。这个 helper 同时服务
    简历草稿生成和对话式自动入库。
    """

    static_response_file = getattr(args, "llm_static_response_file", None)
    static_response = getattr(args, "llm_static_response", None)
    use_env_llm = getattr(args, "use_env_llm", False)
    if static_response_file:
        return StaticLLMClient(Path(static_response_file).read_text(encoding="utf-8"))
    if static_response:
        return StaticLLMClient(static_response)
    if use_env_llm:
        return app.model_gateway.llm_client(
            app.model_gateway.new_call_context(operation, account_id=account_id)
        )
    return None


def run_demo(app: JobHuntingApp, project_path: str, account_id: int | None = None) -> None:
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
        ),
        account_id=account_id,
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
        """,
        account_id=account_id,
    )
    match = app.match_job(candidate_id, job.id, account_id=account_id)
    print_json(
        {
            "candidate_id": candidate_id,
            "project_card": asdict(project_card),
            "job": asdict(job),
            "match": asdict(match),
        }
    )


def run_web_server(args: argparse.Namespace) -> None:
    """启动本地 Web 前端。

    CLI 入口只负责启动服务；真正的 Web API 定义放在 `web.py`，避免命令行模块膨胀。
    """

    from .web import create_web_app

    import uvicorn

    database_settings = load_database_settings(args.env_file)
    if not database_settings.configured or database_settings.url is None:
        raise SystemExit(
            "网页服务需要 JOB_AGENT_DATABASE_URL；请先启动 PostgreSQL 并执行 job-agent database-upgrade。"
        )

    uvicorn.run(
        create_web_app(
            args.db,
            args.env_file,
            args.rag_dir,
            require_auth=True,
            database_url=database_settings.url,
        ),
        host=args.host,
        port=args.port,
    )


def create_admin_account(app: JobHuntingApp) -> None:
    """交互式创建管理员账号；终端密码会直接回显，便于核对输入。"""

    email = input("管理员邮箱: ").strip().lower()
    # 这是本地初始化命令，按用户要求使用普通 input，让密码字符直接显示在终端。
    password = input("管理员密码（至少 8 位，输入内容会显示）: ")
    password_again = input("再次输入密码（输入内容会显示）: ")
    if password != password_again:
        raise SystemExit("两次密码不一致。")
    try:
        account = app.store.create_account(email, hash_password(password), role="admin")
    except ValueError as error:
        raise SystemExit(str(error)) from error
    except Exception as error:  # noqa: BLE001 - CLI 需要把唯一约束错误转成可读信息。
        raise SystemExit(f"管理员创建失败：{error}") from error
    print_json({"status": "ok", "account": asdict(account)})


def resolve_cli_account_id(
    app: JobHuntingApp,
    args: argparse.Namespace,
    *,
    production_database: bool,
) -> int | None:
    """在生产数据库模式下为业务 CLI 强制绑定已存在的账号。"""

    command_needs_account = args.command in ACCOUNT_SCOPED_COMMANDS or (
        args.command == "analyze-project" and args.candidate_id is not None
    )
    if not production_database or not command_needs_account:
        return args.account_id
    if args.account_id is None:
        raise SystemExit(
            f"生产数据库中的 {args.command} 必须在命令前提供 --account-id <账号 ID>。"
        )
    try:
        app.store.get_account(args.account_id)
    except KeyError as error:
        raise SystemExit(f"账号不存在：{args.account_id}") from error
    return args.account_id


def print_json(value: object) -> None:
    """统一 JSON 输出，保留中文，方便人读也方便后续脚本处理。"""

    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
