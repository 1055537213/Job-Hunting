"""LangChain + Chroma RAG 知识库行为测试。

RAG 层只做语义检索索引，不能取代 SQLite 事实源。测试通过 `JobHuntingApp`
公共入口验证：长文本能被索引、能检索出来源、能作为简历草稿证据上下文。
"""

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import CandidateProfileInput


class RecordingLLM:
    """测试用 LLM：记录 prompt，并返回证据内的安全表达。"""

    def __init__(self) -> None:
        """初始化 prompt 记录列表。"""

        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        """返回不包含新增技能/成果数字的安全正文。"""

        self.prompts.append(prompt)
        return "候选人具备 Python 和 FastAPI 项目使用经验，曾负责职位解析和匹配排序相关工作。"


def test_rag_rebuilds_from_long_texts_and_retrieves_source_metadata(tmp_path):
    """系统可以把 SQLite 长文本同步到 Chroma，并检索出带来源的证据。"""

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
    project = tmp_path / "job_agent"
    project.mkdir()
    (project / "README.md").write_text("使用 FastAPI 实现职位解析和匹配排序。", encoding="utf-8")
    card = app.analyze_project_for_candidate(candidate_id, project)
    app.confirm_project_card(card.id, "本人负责 FastAPI 接口、职位解析和匹配排序模块。")

    stats = app.rebuild_rag_index(tmp_path / "chroma")
    results = app.search_rag("FastAPI 职位解析", tmp_path / "chroma", top_k=3)

    assert stats.document_count >= 2
    assert stats.chunk_count >= stats.document_count
    assert results
    assert any("FastAPI" in result.content for result in results)
    assert all(result.entity_type for result in results)
    assert all(result.long_text_id > 0 for result in results)


def test_rag_can_incrementally_index_new_long_texts_without_rebuild(tmp_path):
    """新增长文本可以追加进 Chroma，且不会清空已经索引过的历史资料。"""

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
    first_id = app.store.add_long_text(
        "conversation_message",
        candidate_id,
        "first_note",
        "第一条资料：负责职位解析模块。",
    )
    first_stats = app.index_rag_long_texts([first_id], tmp_path / "chroma")

    second_id = app.store.add_long_text(
        "conversation_message",
        candidate_id,
        "second_note",
        "第二条资料：负责 RAG 知识库和简历草稿生成。",
    )
    second_stats = app.index_rag_long_texts([second_id], tmp_path / "chroma")

    first_results = app.search_rag("职位解析", tmp_path / "chroma")
    second_results = app.search_rag("RAG 知识库 简历草稿", tmp_path / "chroma")

    assert first_stats.mode == "incremental"
    assert second_stats.mode == "incremental"
    assert first_stats.document_count == 1
    assert second_stats.document_count == 1
    assert any("职位解析" in result.content for result in first_results)
    assert any("RAG 知识库" in result.content for result in second_results)


def test_resume_draft_can_use_rag_evidence_without_treating_it_as_profile_fact(tmp_path):
    """简历草稿可以引用 RAG 检索证据，但不会把向量结果写回候选人档案。"""

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
        职位描述：负责 Python、FastAPI 平台开发和职位文本处理。
        """
    )
    project = tmp_path / "job_agent"
    project.mkdir()
    (project / "README.md").write_text("使用 FastAPI 实现职位解析和匹配排序。", encoding="utf-8")
    card = app.analyze_project_for_candidate(candidate_id, project)
    app.confirm_project_card(card.id, "本人负责 FastAPI 接口、职位解析和匹配排序模块。")
    app.rebuild_rag_index(tmp_path / "chroma")

    llm = RecordingLLM()
    draft = app.create_resume_draft(
        candidate_id,
        job.id,
        llm_client=llm,
        rag_persist_directory=tmp_path / "chroma",
        rag_query="FastAPI 职位解析 匹配排序",
    )

    assert llm.prompts
    assert "RAG 检索证据" in llm.prompts[0]
    assert any("RAG 检索证据" in item for item in draft.draft.evidence_items)
    assert "FastAPI" in draft.draft.content
    assert app.get_candidate_profile(candidate_id).skills == {"Python": "项目使用", "FastAPI": "项目使用"}
