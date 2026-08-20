"""对话式自动入库行为测试。

用户希望“把资料发给 agent 后，agent 一边回复，一边自动判断哪些内容进入
PostgreSQL 结构化事实、哪些内容进入 RAG 长文本知识库”。这一组测试验证第一版
自动入库链路。
"""

import json

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.conversation_ingestion import decision_from_json
from job_hunting_agent.models import CandidateProfileInput, sanitize_preference_weights


def test_conversation_message_auto_saves_structured_facts_and_long_text(tmp_path, account_id):
    """一条资料消息可以自动更新候选人档案，并保存原文长文本。"""

    app = JobHuntingApp()
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
        ),
        account_id=account_id,
    )

    result = app.ingest_conversation_message(
        candidate_id,
        "我是本科，1年经验，会 Python 和 FastAPI。做过一个求职助手项目，负责职位解析和匹配排序。",
        account_id=account_id,
    )
    updated_profile = app.get_candidate_profile(candidate_id, account_id=account_id)
    long_texts = app.store.list_long_texts(["conversation_message"], account_id=account_id)

    assert "已保存" in result.reply
    assert updated_profile.education == "本科"
    assert updated_profile.experience_years == 1
    assert updated_profile.skills["Python"] == "待确认"
    assert updated_profile.skills["FastAPI"] == "待确认"
    assert "education" in result.saved_structured_fields
    assert "skills" in result.saved_structured_fields
    assert result.saved_long_text_ids
    assert long_texts[0].text.startswith("我是本科")


def test_conversation_message_can_auto_incrementally_index_rag(tmp_path, account_id):
    """自动入库后可以增量追加 RAG 索引，让新资料立刻可检索。"""

    app = JobHuntingApp()
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
        ),
        account_id=account_id,
    )

    result = app.ingest_conversation_message(
        candidate_id,
        "项目经历：我做过一个 Agent 求职助手，负责 RAG 知识库、职位解析和简历草稿生成。",
        auto_rebuild_rag=True,
        account_id=account_id,
    )
    search_results = app.search_rag("RAG 知识库 简历草稿", account_id=account_id)

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


def test_conversation_ingestion_can_use_llm_json_decision(tmp_path, account_id):
    """自动入库可以使用 LLM 的 JSON 决策，但仍通过本地存储边界落库。"""

    app = JobHuntingApp()
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
            preference_weights={"salary": 2.0},
        ),
        account_id=account_id,
    )

    llm = DecisionFakeLLM()
    result = app.ingest_conversation_message(
        candidate_id,
        "我最近做了 LangChain RAG 项目。",
        llm_client=llm,
        account_id=account_id,
    )
    updated_profile = app.get_candidate_profile(candidate_id, account_id=account_id)

    assert llm.prompts
    assert result.reply == "我已提取并保存你的学历、技能和项目材料。"
    assert updated_profile.education == "硕士"
    assert updated_profile.skills["LangChain"] == "项目使用"
    assert updated_profile.target_directions == ["AI Agent 应用开发"]
    # LLM 本轮没有返回偏好字段时，不能把档案中已有的薪资权重重置为默认值。
    assert updated_profile.preference_weights["salary"] == 2.0
    assert result.saved_long_text_ids


def test_conversation_persists_only_explicit_preference_weight_updates(tmp_path, account_id):
    """明确优先级只覆盖提到的维度，不能把默认权重误写成更新。"""

    app = JobHuntingApp()
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="权重测试",
            status="待补充",
            education="本科",
            experience_years=1,
            skills={},
            preferred_cities=["杭州"],
            salary_floor_k=10,
            expected_salary_k=15,
            target_directions=["Python 后端"],
            unacceptable=[],
        ),
        account_id=account_id,
    )

    result = app.ingest_conversation_message(
        candidate_id,
        "我最看重薪资，城市无所谓。",
        account_id=account_id,
    )
    profile = app.get_candidate_profile(candidate_id, account_id=account_id)

    assert profile.preference_weights["salary"] == 2.0
    assert profile.preference_weights["city"] == 1.0
    assert "preference_weights" in result.saved_structured_fields
    assert profile.preference_weights["skills"] == 1.0
    assert profile.preferred_cities == []
    assert profile.acceptable_cities == []


def test_conversation_direction_replacement_overwrites_previous_direction(tmp_path, account_id):
    """“求职方向改为 X”必须替换旧方向，不能把 X 追加到旧列表。"""

    app = JobHuntingApp()
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="方向替换测试",
            status="待补充",
            education="本科",
            experience_years=1,
            skills={},
            preferred_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=["AI Agent 应用开发"],
            unacceptable=[],
        ),
        account_id=account_id,
    )

    result = app.ingest_conversation_message(
        candidate_id,
        "我想把求职方向改为后端开发。",
        account_id=account_id,
    )
    profile = app.get_candidate_profile(candidate_id, account_id=account_id)

    assert "target_directions" in result.saved_structured_fields
    assert profile.target_directions == ["后端开发"]


def test_conversation_direction_replacement_accepts_other_common_directions(tmp_path, account_id):
    """替换方向不能只对 Agent 和后端两个硬编码值生效。"""

    app = JobHuntingApp()
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="前端方向替换测试",
            status="待补充",
            education="本科",
            experience_years=1,
            skills={},
            preferred_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=["AI Agent 应用开发"],
            unacceptable=[],
        ),
        account_id=account_id,
    )

    app.ingest_conversation_message(
        candidate_id,
        "请把我的求职方向改成前端开发。",
        account_id=account_id,
    )

    profile = app.get_candidate_profile(candidate_id, account_id=account_id)
    assert profile.target_directions == ["前端开发"]


def test_conversation_direction_addition_keeps_previous_direction(tmp_path, account_id):
    """“也考虑 X”是补充方向，必须保留既有方向。"""

    app = JobHuntingApp()
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="方向追加测试",
            status="待补充",
            education="本科",
            experience_years=1,
            skills={},
            preferred_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=["AI Agent 应用开发"],
            unacceptable=[],
        ),
        account_id=account_id,
    )

    app.ingest_conversation_message(
        candidate_id,
        "我也考虑后端开发。",
        account_id=account_id,
    )

    profile = app.get_candidate_profile(candidate_id, account_id=account_id)
    assert profile.target_directions == ["AI Agent 应用开发", "后端开发"]


class DirectionReplacementFakeLLM:
    """故意返回旧方向，验证本地替换意图能够约束模型决策。"""

    def complete(self, prompt: str) -> str:
        return """
        {
          "reply": "已更新求职方向。",
          "profile_updates": {
            "target_directions": ["AI Agent 应用开发", "后端开发"]
          },
          "long_texts": []
        }
        """


def test_llm_ingestion_direction_replacement_does_not_restore_old_direction(tmp_path, account_id):
    """模型即使错误回传旧方向，明确替换意图也必须以用户原话为准。"""

    app = JobHuntingApp()
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="模型方向替换测试",
            status="待补充",
            education="本科",
            experience_years=1,
            skills={},
            preferred_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=["AI Agent 应用开发"],
            unacceptable=[],
        ),
        account_id=account_id,
    )

    app.ingest_conversation_message(
        candidate_id,
        "我想把求职方向改为后端开发。",
        llm_client=DirectionReplacementFakeLLM(),
        account_id=account_id,
    )

    profile = app.get_candidate_profile(candidate_id, account_id=account_id)
    assert profile.target_directions == ["后端开发"]


def test_preference_weights_are_limited_to_discrete_levels():
    """外部或旧数据中的任意小数权重会被归一化到三个支持等级。"""

    weights = sanitize_preference_weights({"salary": 1.7, "city": 1.2})

    assert weights["salary"] == 1.5
    assert weights["city"] == 1.0


def test_rule_based_ingestion_preserves_explicit_missing_skill(tmp_path, account_id):
    """候选人明确说不会某技能时，结构化档案需要保留负向事实。"""

    app = JobHuntingApp()
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="负向技能测试",
            status="待补充",
            education="本科",
            experience_years=1,
            skills={},
            preferred_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=[],
            unacceptable=[],
        ),
        account_id=account_id,
    )

    app.ingest_conversation_message(
        candidate_id,
        "我不会 Docker，但会 Python。",
        account_id=account_id,
    )

    profile = app.get_candidate_profile(candidate_id, account_id=account_id)
    assert profile.skills["Docker"] == "不会"
    assert profile.skills["Python"] == "待确认"


def test_rule_based_ingestion_saves_explicit_skill_proficiency(tmp_path, account_id):
    """明确说明技能熟练度时，档案摘要应保存用户说出的等级。"""

    app = JobHuntingApp()
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="技能熟练度测试",
            status="待补充",
            education="本科",
            experience_years=1,
            skills={"Python": "待确认"},
            preferred_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=[],
            unacceptable=[],
        ),
        account_id=account_id,
    )

    app.ingest_conversation_message(
        candidate_id,
        "我的python熟练度是精通。",
        account_id=account_id,
    )

    profile = app.get_candidate_profile(candidate_id, account_id=account_id)
    assert profile.skills["Python"] == "精通"


def test_llm_ingestion_uses_original_message_for_explicit_skill_proficiency(tmp_path, account_id):
    """LLM 即使把明确熟练度回传成待确认，也不能覆盖用户原话。"""

    app = JobHuntingApp()
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="LLM 技能熟练度测试",
            status="待补充",
            education="本科",
            experience_years=1,
            skills={"Python": "待确认"},
            preferred_cities=[],
            salary_floor_k=None,
            expected_salary_k=None,
            target_directions=[],
            unacceptable=[],
        ),
        account_id=account_id,
    )
    candidate = app.get_candidate_profile(candidate_id, account_id=account_id)
    decision = decision_from_json(
        candidate,
        "我的python熟练度是精通。",
        json.dumps(
            {
                "reply": "已记录。",
                "profile_updates": {"skills": {"Python": "待确认"}},
                "long_texts": [],
            },
            ensure_ascii=False,
        ),
    )

    app.store.update_candidate_profile(
        candidate_id,
        decision.profile_updates,
        account_id=account_id,
    )

    profile = app.get_candidate_profile(candidate_id, account_id=account_id)
    assert profile.skills["Python"] == "精通"
