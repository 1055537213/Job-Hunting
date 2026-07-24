"""证据约束简历草稿测试。

这一组测试把 LLM 当成可替换的表达工具，而不是事实源。即使 LLM 输出了
候选人档案里没有的技能或成果，系统也必须保留真实性边界。
"""

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import CandidateProfileInput


class UnsafeFakeLLM:
    """测试用假 LLM：故意输出未确认事实，验证安全回退逻辑。"""

    def __init__(self) -> None:
        """记录收到的 prompt，方便测试确认业务层确实调用过 LLM。"""

        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        """返回一段不可信输出，模拟真实 LLM 可能出现的夸写。"""

        self.prompts.append(prompt)
        return "候选人精通 Kubernetes，并通过架构优化将性能提升 50%。"


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
