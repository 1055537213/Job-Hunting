"""MVP 端到端行为测试。

这些测试只通过 `JobHuntingApp` 这个公共入口验证行为，不直接测试内部私有函数。
这样以后内部实现从规则版替换成 LLM/向量库时，只要外部行为不变，测试仍然有价值。
"""

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import CandidateProfileInput


def test_candidate_can_import_job_and_get_explainable_match(tmp_path):
    """候选人可以导入职位文本，并得到可解释匹配结果。"""

    app = JobHuntingApp(tmp_path / "mvp.db")
    app.initialize()

    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
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

    job = app.import_job_text(
        # 这段文本模拟候选人从 BOSS 页面复制回来的可见职位信息。
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
        source_url="https://www.zhipin.com/job_detail/example.html",
    )

    match = app.match_job(candidate_id, job.id)

    assert match.tier in {"强推荐", "可投递"}
    assert match.score >= 65
    assert not match.eliminated
    assert any("Python" in item for item in match.reasons)
    assert match.resume_suggestions


def test_matcher_applies_experience_and_education_hard_eliminations(tmp_path):
    """匹配器会执行 #7 定下的经验和学历硬性淘汰规则。"""

    app = JobHuntingApp(tmp_path / "mvp.db")
    app.initialize()

    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="在职",
            education="本科",
            experience_years=1.0,
            skills={"Python": "项目使用"},
            preferred_cities=["杭州"],
            salary_floor_k=8,
            expected_salary_k=12,
            target_directions=["Python 后端"],
            unacceptable=[],
        )
    )

    senior_job = app.import_job_text(
        """
        Python 后端开发工程师
        15-25K
        杭州
        5-10年
        本科
        职位描述：负责 Python 后端系统开发。
        """
    )
    education_job = app.import_job_text(
        """
        AI 算法工程师
        20-30K
        杭州
        1-3年
        硕士
        职位描述：负责 Python 和 RAG 应用。
        """
    )

    senior_match = app.match_job(candidate_id, senior_job.id)
    education_match = app.match_job(candidate_id, education_job.id)

    assert senior_match.eliminated
    assert any("经验" in reason for reason in senior_match.elimination_reasons)
    assert education_match.eliminated
    assert any("学历" in reason for reason in education_match.elimination_reasons)


def test_target_city_preference_changes_ranking_without_eliminating_job(tmp_path):
    """目标城市属于普通偏好，异地职位只能扣分，不能被当成明确拒绝条件。"""

    app = JobHuntingApp(tmp_path / "mvp.db")
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="离职",
            education="本科",
            experience_years=2,
            skills={"Python": "项目使用"},
            preferred_cities=["杭州"],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=["Python 开发"],
            unacceptable=[],
        )
    )
    job = app.import_job_text(
        """
        Python 开发工程师
        15-20K
        上海
        1-3年
        本科
        职位描述：负责 Python 后端开发。
        """
    )

    result = app.match_job(candidate_id, job.id)

    assert not result.eliminated
    assert any("目标城市偏好" in item for item in result.deductions)
    assert any("需要确认是否接受" in item for item in result.risks)


def test_project_analysis_outputs_confirmable_card_and_skips_sensitive_files(tmp_path):
    """项目分析会生成待确认卡片，并跳过 .env 等敏感文件。"""

    project = tmp_path / "demo_agent"
    project.mkdir()
    (project / "README.md").write_text(
        "基于 LangChain 和 RAG 的求职助手 Agent，使用 FastAPI 提供接口。",
        encoding="utf-8",
    )
    (project / "requirements.txt").write_text(
        "langchain\nfastapi\nchromadb\npytest\n",
        encoding="utf-8",
    )
    (project / "app.py").write_text(
        "from fastapi import FastAPI\nfrom langchain_core.messages import HumanMessage\n",
        encoding="utf-8",
    )
    (project / ".env").write_text("OPENAI_API_KEY=should-not-read", encoding="utf-8")

    app = JobHuntingApp(tmp_path / "mvp.db")
    app.initialize()
    card = app.analyze_project(project)

    assert card.card_type == "待确认项目经历卡片"
    assert {"LangChain", "FastAPI", "RAG"} <= set(card.detected_tech_stack)
    assert ".env" not in card.read_files
    assert card.skipped_summary["sensitive_name"] == 1
    assert card.questions_for_candidate


def test_project_card_can_be_saved_and_confirmed_without_overwriting_profile(tmp_path):
    """项目分析结果要先作为待确认卡片保存，不能自动覆盖候选人档案事实。"""

    project = tmp_path / "portfolio_agent"
    project.mkdir()
    (project / "README.md").write_text(
        "基于 LangChain、FastAPI 和 RAG 的求职助手 Agent。",
        encoding="utf-8",
    )
    (project / "app.py").write_text(
        "from fastapi import FastAPI\nfrom langchain_core.messages import HumanMessage\n",
        encoding="utf-8",
    )

    app = JobHuntingApp(tmp_path / "mvp.db")
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="离职",
            education="本科",
            experience_years=1.0,
            skills={"Python": "项目使用"},
            preferred_cities=["杭州"],
            salary_floor_k=10,
            expected_salary_k=15,
            target_directions=["AI Agent 应用开发"],
            unacceptable=[],
        )
    )

    pending_record = app.analyze_project_for_candidate(candidate_id, project)
    profile_after_analysis = app.get_candidate_profile(candidate_id)

    assert pending_record.status == "待确认"
    assert pending_record.candidate_id == candidate_id
    assert pending_record.card.project_name == "portfolio_agent"
    # 自动分析发现的 LangChain/FastAPI 只能进入卡片，不能直接写入候选人技能事实。
    assert profile_after_analysis.skills == {"Python": "项目使用"}

    confirmed_record = app.confirm_project_card(
        pending_record.id,
        confirmed_summary="本人负责职位解析、匹配排序和 FastAPI 接口设计。",
    )
    listed_records = app.list_project_cards(candidate_id)

    assert confirmed_record.status == "已确认"
    assert confirmed_record.confirmed_summary == "本人负责职位解析、匹配排序和 FastAPI 接口设计。"
    assert [record.id for record in listed_records] == [pending_record.id]
    assert listed_records[0].status == "已确认"


def test_candidate_can_match_all_imported_jobs_in_recommendation_order(tmp_path):
    """候选人可以对所有已导入职位批量匹配，未淘汰职位按分数优先展示。"""

    app = JobHuntingApp(tmp_path / "mvp.db")
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="离职",
            education="本科",
            experience_years=1.0,
            skills={"Python": "项目使用", "FastAPI": "项目使用", "LangChain": "项目使用"},
            preferred_cities=["杭州"],
            salary_floor_k=10,
            expected_salary_k=15,
            target_directions=["AI Agent 应用开发"],
            unacceptable=["外包"],
        )
    )
    strong_job = app.import_job_text(
        """
        Python Agent 应用开发工程师
        15-25K
        杭州
        1-3年
        本科
        职位描述：负责 Python、FastAPI、LangChain Agent 应用开发。
        """
    )
    weak_job = app.import_job_text(
        """
        Java 后端开发工程师
        12-16K
        杭州
        1-3年
        本科
        职位描述：负责 Java、MySQL 和 Redis 后端开发。
        """
    )
    eliminated_job = app.import_job_text(
        """
        资深 Python 后端开发工程师
        20-30K
        杭州
        5-10年
        本科
        职位描述：负责 Python 后端架构设计。
        """
    )

    matches = app.match_all_jobs(candidate_id)

    assert [match.job_id for match in matches] == [strong_job.id, weak_job.id, eliminated_job.id]
    assert not matches[0].eliminated
    assert matches[0].score > matches[1].score
    assert matches[-1].eliminated
