"""证据约束简历草稿测试。

这一组测试把 LLM 当成可替换的表达工具，而不是事实源。即使 LLM 输出了
候选人档案里没有的技能或成果，系统也必须保留真实性边界。
"""

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import CandidateProfileInput
from job_hunting_agent.resume_writer import build_resume_draft


class UnsafeFakeLLM:
    """测试用假 LLM：故意输出未确认事实，验证安全回退逻辑。"""

    def __init__(self) -> None:
        """记录收到的 prompt，方便测试确认业务层确实调用过 LLM。"""

        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        """返回一段不可信输出，模拟真实 LLM 可能出现的夸写。"""

        self.prompts.append(prompt)
        return "候选人精通 Kubernetes，并通过架构优化将性能提升 50%。"


class JobContextPollutingFakeLLM:
    """测试用假 LLM：把职位上下文伪装成候选人的技能和成果。"""

    def complete(self, prompt: str) -> str:
        """返回只存在于职位/RAG 上下文中的未确认能力。"""

        return "候选人精通 FastAPI，并通过接口优化将性能提升 50%。"


class InflatedKnownSkillFakeLLM:
    """测试用假 LLM：把候选人仅了解的技能改写成精通。"""

    def complete(self, prompt: str) -> str:
        """返回熟练度高于候选人档案的技能表述。"""

        return "候选人精通 Python，可负责相关开发工作。"


def test_llm_resume_draft_discards_unsupported_claims_and_saves_version(tmp_path):
    """LLM 简历草稿不能引入未确认技能或成果，并会保存为职位定制版本。"""

    app = JobHuntingApp(tmp_path / "mvp.db")
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="离职",
            education="本科",
            experience_years=1.0,
            skills={"Python": "项目使用", "FastAPI": "项目使用"},
            preferred_cities=["杭州"],
            salary_floor_k=10,
            expected_salary_k=15,
            target_directions=["Python 后端开发"],
            unacceptable=[],
        )
    )
    job = app.import_job_text(
        """
        Python 平台开发工程师
        15-22K
        杭州
        1-3年
        本科
        职位描述：负责 Python、FastAPI、Kubernetes 平台开发。
        """
    )
    project = tmp_path / "job_agent"
    project.mkdir()
    (project / "README.md").write_text(
        "基于 Python 和 FastAPI 的求职助手项目。",
        encoding="utf-8",
    )
    card = app.analyze_project_for_candidate(candidate_id, project)
    app.confirm_project_card(card.id, "本人负责职位解析、匹配排序和 FastAPI 接口设计。")

    fake_llm = UnsafeFakeLLM()
    draft_record = app.create_resume_draft(candidate_id, job.id, llm_client=fake_llm)
    saved_records = app.list_resume_drafts(candidate_id, job.id)

    assert fake_llm.prompts
    assert draft_record.status == "需候选人确认"
    assert draft_record.version == 1
    assert saved_records[0].id == draft_record.id
    assert "Python 平台开发工程师" in draft_record.draft.title
    assert "Python" in draft_record.draft.content
    assert "FastAPI" in draft_record.draft.content
    # 未确认技能和夸大的成果数字不能进入最终可编辑草稿正文。
    assert "Kubernetes" not in draft_record.draft.content
    assert "提升 50%" not in draft_record.draft.content
    assert any("未确认技能：Kubernetes" in risk for risk in draft_record.draft.authenticity_risks)
    assert any("LLM 输出已丢弃" in risk for risk in draft_record.draft.authenticity_risks)
    assert any("本人负责职位解析" in item for item in draft_record.draft.evidence_items)
    # 简历草稿是单独版本，不会反向覆盖候选人档案技能。
    assert app.get_candidate_profile(candidate_id).skills == {"Python": "项目使用", "FastAPI": "项目使用"}


def test_job_rag_context_cannot_establish_candidate_skills_or_metrics(tmp_path):
    """职位或 RAG 上下文不能绕过候选人事实边界，也不能让规则回退崩溃。"""

    app = JobHuntingApp(tmp_path / "mvp.db")
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="离职",
            education="本科",
            experience_years=1,
            skills={"Python": "项目使用"},
            preferred_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=[],
            unacceptable=[],
        )
    )
    job = app.import_job_text(
        """
        平台开发工程师
        15-20K
        杭州
        1-3年
        本科
        职位描述：要求使用 FastAPI 开发接口，并将性能提升 50%。
        """
    )
    candidate = app.get_candidate_profile(candidate_id)
    semantic_context = ["RAG 职位上下文：要求 FastAPI，并将性能提升 50%。"]

    fallback = build_resume_draft(candidate, job, [], semantic_evidence=semantic_context)
    rewritten = build_resume_draft(
        candidate,
        job,
        [],
        llm_client=JobContextPollutingFakeLLM(),
        semantic_evidence=semantic_context,
    )

    assert "候选人材料中提及 FastAPI" not in fallback.content
    assert rewritten.llm_discarded
    assert "精通 FastAPI" not in rewritten.content
    assert any("未确认技能：FastAPI" in risk for risk in rewritten.authenticity_risks)


def test_proficiency_override_requires_explicit_flag_and_keeps_risk(tmp_path):
    """默认阻止熟练度拔高；明确的一次性覆盖也必须保留真实性风险。"""

    app = JobHuntingApp(tmp_path / "mvp.db")
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="离职",
            education="本科",
            experience_years=1,
            skills={"Python": "了解"},
            preferred_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=[],
            unacceptable=[],
        )
    )
    job = app.import_job_text(
        """
        Python 开发工程师
        10-15K
        杭州
        经验不限
        本科
        职位描述：负责 Python 开发。
        """
    )
    candidate = app.get_candidate_profile(candidate_id)

    conservative = build_resume_draft(candidate, job, [], llm_client=InflatedKnownSkillFakeLLM())
    explicit_override = build_resume_draft(
        candidate,
        job,
        [],
        llm_client=InflatedKnownSkillFakeLLM(),
        allow_proficiency_upgrade=True,
    )

    assert conservative.llm_discarded
    assert not explicit_override.llm_discarded
    assert any("明确要求放宽熟练度" in risk for risk in explicit_override.authenticity_risks)
    assert candidate.skills == {"Python": "了解"}
