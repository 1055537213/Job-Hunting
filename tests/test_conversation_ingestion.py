"""对话式自动入库行为测试。

用户希望“把资料发给 agent 后，agent 一边回复，一边自动判断哪些内容进入
SQLite 结构化事实、哪些内容进入 RAG 长文本知识库”。这一组测试验证第一版
自动入库链路。
"""

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import CandidateProfileInput


def test_conversation_message_auto_saves_structured_facts_and_long_text(tmp_path):
    """一条资料消息可以自动更新候选人档案，并保存原文长文本。"""

    app = JobHuntingApp(tmp_path / "mvp.db")
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="待补充",
            education="大专",
            experience_years=0,
            skills={},
            preferred_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=[],
            unacceptable=[],
        )
    )

    result = app.ingest_conversation_message(
        candidate_id,
        "我是本科，1年经验，会 Python 和 FastAPI。做过一个求职助手项目，负责职位解析和匹配排序。",
    )
    updated_profile = app.get_candidate_profile(candidate_id)
    long_texts = app.store.list_long_texts(["conversation_message"])

    assert "已保存" in result.reply
    assert updated_profile.education == "本科"
    assert updated_profile.experience_years == 1
    assert updated_profile.skills["Python"] == "待确认"
    assert updated_profile.skills["FastAPI"] == "待确认"
    assert "education" in result.saved_structured_fields
    assert "skills" in result.saved_structured_fields
    assert result.saved_long_text_ids
    assert long_texts[0].text.startswith("我是本科")


def test_conversation_message_can_auto_incrementally_index_rag(tmp_path):
    """自动入库后可以增量追加 RAG 索引，让新资料立刻可检索。"""

    app = JobHuntingApp(tmp_path / "mvp.db")
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="待补充",
            education="本科",
            experience_years=0,
            skills={},
            preferred_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=[],
            unacceptable=[],
        )
    )

    result = app.ingest_conversation_message(
        candidate_id,
        "项目经历：我做过一个 Agent 求职助手，负责 RAG 知识库、职位解析和简历草稿生成。",
        rag_persist_directory=tmp_path / "chroma",
        auto_rebuild_rag=True,
    )
    search_results = app.search_rag("RAG 知识库 简历草稿", tmp_path / "chroma")

    assert not result.rag_rebuilt
    assert result.rag_update_mode == "incremental"
    assert result.rag_index_stats is not None
    assert result.rag_index_stats.mode == "incremental"
    assert search_results
    assert any("RAG 知识库" in item.content for item in search_results)


class DecisionFakeLLM:
    """测试用 LLM：返回结构化 JSON 保存决策。"""

    def __init__(self) -> None:
        """记录 prompt，确认应用层确实调用了 LLM。"""

        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        """返回一段 LLM 决策 JSON。"""

        self.prompts.append(prompt)
        return """
        {
          "reply": "我已提取并保存你的学历、技能和项目材料。",
          "profile_updates": {
            "education": "硕士",
            "skills": {"LangChain": "项目使用", "RAG": "项目使用"},
            "target_directions": ["AI Agent 应用开发"]
          },
          "long_texts": [
            {
              "source_label": "llm_extracted_project_note",
              "text": "候选人做过 LangChain 和 RAG 相关项目，目标方向是 AI Agent 应用开发。"
            }
          ]
        }
        """


def test_conversation_ingestion_can_use_llm_json_decision(tmp_path):
    """自动入库可以使用 LLM 的 JSON 决策，但仍通过本地存储边界落库。"""

    app = JobHuntingApp(tmp_path / "mvp.db")
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="待补充",
            education="本科",
            experience_years=1,
            skills={},
            preferred_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=[],
            unacceptable=[],
        )
    )

    llm = DecisionFakeLLM()
    result = app.ingest_conversation_message(candidate_id, "我最近做了 LangChain RAG 项目。", llm_client=llm)
    updated_profile = app.get_candidate_profile(candidate_id)

    assert llm.prompts
    assert result.reply == "我已提取并保存你的学历、技能和项目材料。"
    assert updated_profile.education == "硕士"
    assert updated_profile.skills["LangChain"] == "项目使用"
    assert updated_profile.target_directions == ["AI Agent 应用开发"]
    assert result.saved_long_text_ids
