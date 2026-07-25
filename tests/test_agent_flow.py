"""标准 LangChain Agent 行为测试。

这里不访问真实网络，而是注入一个支持工具调用的假 ChatModel，验证：

- Agent 会按标准 `create_agent -> tool loop` 执行。
- 工具不会越过 `JobHuntingApp` 直接改库。
- Agent 工具执行后，SQLite / RAG 的业务结果确实落地。
"""

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from job_hunting_agent.agent import JobHuntingAgent
from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import CandidateProfileInput


class ToolCallingFakeChatModel(FakeMessagesListChatModel):
    """测试用假模型：支持 `create_agent` 的工具绑定。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # noqa: ANN001,D401
        """直接返回自身，让测试可以手工指定工具调用序列。"""

        return self


def test_langchain_agent_can_call_ingest_tool_and_update_profile(tmp_path):
    """Agent 可以通过工具把聊天资料保存进 SQLite，并增量更新 RAG。"""

    app = JobHuntingApp(tmp_path / "agent.db")
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
    model = ToolCallingFakeChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "ingest_candidate_message",
                        "args": {
                            "message": "我是本科，1年经验，会 Python 和 FastAPI。做过一个求职助手项目。",
                            "auto_rag": True,
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="我已经保存你的资料，并整理成后续可检索的候选人材料。"),
        ]
    )

    agent = JobHuntingAgent(app, rag_dir=tmp_path / "chroma", model=model)
    result = agent.chat(
        "我是本科，1年经验，会 Python 和 FastAPI。做过一个求职助手项目。",
        candidate_id=candidate_id,
        session_id="agent-test-ingest",
        use_tool_llm=False,
        auto_rag=True,
    )
    profile = app.get_candidate_profile(candidate_id)
    rag_results = app.search_rag("FastAPI 求职助手", tmp_path / "chroma")

    assert result.mode == "langchain_agent"
    assert result.used_tools == ["ingest_candidate_message"]
    assert "保存" in result.reply
    assert profile.education == "本科"
    assert profile.experience_years == 1
    assert profile.skills["Python"] == "待确认"
    assert any(item["tool_name"] == "ingest_candidate_message" for item in result.tool_outputs)
    assert any("FastAPI" in item.content for item in rag_results)


def test_langchain_agent_can_loop_across_multiple_tools(tmp_path):
    """Agent 可以先导入职位，再继续调用匹配工具，完成多步工具循环。"""

    app = JobHuntingApp(tmp_path / "agent.db")
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="离职",
            education="本科",
            experience_years=1,
            skills={"Python": "项目使用", "FastAPI": "项目使用"},
            preferred_cities=["杭州"],
            salary_floor_k=10,
            expected_salary_k=15,
            target_directions=["Python 后端开发"],
            unacceptable=[],
        )
    )
    model = ToolCallingFakeChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "import_job_from_text",
                        "args": {
                            "raw_text": """
                            Python 后端开发工程师
                            15-20K
                            杭州
                            1-3年
                            本科
                            职位描述：负责 Python 和 FastAPI 后端开发。
                            """,
                            "source_url": "https://www.zhipin.com/job_detail/example.html",
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_2",
                        "name": "match_all_jobs_for_candidate",
                        "args": {},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="职位已经导入完成，我也帮你做了本地职位匹配。"),
        ]
    )

    agent = JobHuntingAgent(app, rag_dir=tmp_path / "chroma", model=model)
    result = agent.chat(
        "请帮我导入这个职位并判断我适不适合投。",
        candidate_id=candidate_id,
        session_id="agent-test-loop",
        use_tool_llm=False,
    )

    assert result.used_tools == ["import_job_from_text", "match_all_jobs_for_candidate"]
    assert len(app.list_jobs()) == 1
    assert any(output["tool_name"] == "match_all_jobs_for_candidate" for output in result.tool_outputs)
