"""标准 LangChain Agent 行为测试。

这里不访问真实网络，而是注入一个支持工具调用的假 ChatModel，验证：

- Agent 会按标准 `create_agent -> tool loop` 执行。
- 工具不会越过 `JobHuntingApp` 直接改库。
- Agent 工具执行后，SQLite / RAG 的业务结果确实落地。
"""

from io import BytesIO

from docx import Document
from langchain_core.language_models.fake_chat_models import FakeListChatModel, FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import Field

from job_hunting_agent.config import AgentMemorySettings
from job_hunting_agent.agent import JobHuntingAgent
from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import CandidateProfileInput


def build_resume_docx_bytes(*paragraphs: str) -> bytes:
    """创建 Agent 文件工具测试使用的内存 DOCX。"""

    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


class ToolCallingFakeChatModel(FakeMessagesListChatModel):
    """测试用假模型：支持 `create_agent` 的工具绑定。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # noqa: ANN001,D401
        """直接返回自身，让测试可以手工指定工具调用序列。"""

        return self


class RecordingToolCallingFakeChatModel(ToolCallingFakeChatModel):
    """测试用假模型：记录每次真正传给模型的消息列表。"""

    seen_messages: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        """记录输入后继续使用父类的固定响应。"""

        self.seen_messages.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class StreamingFakeChatModel(FakeListChatModel):
    """测试用流式假模型：每个字符会作为一个 AIMessageChunk 输出。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # noqa: ANN001,D401
        """直接返回自身，让 `create_agent` 保留模型的 `_stream` 行为。"""

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


def test_langchain_agent_can_stream_reply_events(tmp_path):
    """Agent 可以通过 LangGraph stream 产出 token 和 final 事件。"""

    app = JobHuntingApp(tmp_path / "agent.db")
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="离职",
            education="本科",
            experience_years=1,
            skills={"Python": "项目使用"},
            preferred_cities=["杭州"],
            salary_floor_k=10,
            expected_salary_k=15,
            target_directions=["Python 后端开发"],
            unacceptable=[],
        )
    )
    model = ToolCallingFakeChatModel(responses=[AIMessage(content="可以，我会用流式方式回复。")])
    agent = JobHuntingAgent(app, rag_dir=tmp_path / "chroma", model=model)

    events = list(
        agent.stream_chat(
            "用 stream 输出。",
            candidate_id=candidate_id,
            session_id="agent-test-stream",
            use_tool_llm=False,
        )
    )

    assert any(event["type"] == "token" for event in events)
    assert events[-1]["type"] == "final"
    assert events[-1]["result"].reply == "可以，我会用流式方式回复。"


def test_langchain_agent_streams_multiple_token_events_with_streaming_model(tmp_path):
    """当底层模型支持 token stream 时，Agent 不应退化成一次性完整回复。"""

    app = JobHuntingApp(tmp_path / "agent.db")
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="离职",
            education="本科",
            experience_years=1,
            skills={"Python": "项目使用"},
            preferred_cities=["杭州"],
            salary_floor_k=10,
            expected_salary_k=15,
            target_directions=["Python 后端开发"],
            unacceptable=[],
        )
    )
    model = StreamingFakeChatModel(responses=["流式OK"])
    agent = JobHuntingAgent(app, rag_dir=tmp_path / "chroma", model=model)

    events = list(
        agent.stream_chat(
            "请流式回复。",
            candidate_id=candidate_id,
            session_id="agent-test-stream-chunks",
            use_tool_llm=False,
        )
    )
    token_events = [event for event in events if event["type"] == "token"]

    assert [event["content"] for event in token_events] == ["流", "式", "O", "K"]
    assert events[-1]["type"] == "final"
    assert events[-1]["result"].reply == "流式OK"


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


def test_langchain_agent_can_create_downloadable_resume_files_from_upload(tmp_path):
    """Agent 能先查看上传文件，再生成职位定制 DOCX/PDF 下载版本。"""

    app = JobHuntingApp(
        tmp_path / "agent.db",
        resume_dir=tmp_path / "resume-files",
    )
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="离职",
            education="本科",
            experience_years=1,
            skills={"Python": "项目使用"},
            preferred_cities=["杭州市"],
            salary_floor_k=10,
            expected_salary_k=15,
            target_directions=["Python 后端开发"],
            unacceptable=[],
        )
    )
    job = app.import_job_text(
        """
        Python 后端开发工程师
        15-20K
        杭州
        1-3年
        本科
        职位描述：负责 Python 与 FastAPI 后端接口开发。
        """
    )
    source = app.upload_resume_document(
        candidate_id,
        "resume.docx",
        build_resume_docx_bytes("小林", "Python 与 FastAPI 项目经历"),
    )
    model = ToolCallingFakeChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_resume_list",
                        "name": "list_resume_artifacts_for_candidate",
                        "args": {},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_resume_tailor",
                        "name": "create_tailored_resume_from_upload",
                        "args": {
                            "source_artifact_id": source.id,
                            "job_id": job.id,
                            "use_rag": False,
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="已生成职位定制简历，你可以下载 DOCX 或 PDF。"),
        ]
    )

    result = JobHuntingAgent(app, rag_dir=tmp_path / "chroma", model=model).chat(
        "用我上传的简历针对这个 Python 职位生成可下载文件。",
        candidate_id=candidate_id,
        session_id="agent-resume-file",
        use_tool_llm=False,
    )
    artifacts = app.list_resume_artifacts(candidate_id)

    assert result.used_tools == [
        "list_resume_artifacts_for_candidate",
        "create_tailored_resume_from_upload",
    ]
    assert len([item for item in artifacts if item.artifact_type == "tailored"]) == 2
    generated_output = result.tool_outputs[-1]["data"]
    assert len(generated_output["artifacts"]) == 2
    assert all("download_url" in item for item in generated_output["artifacts"])
    assert all("storage_key" not in item for item in generated_output["artifacts"])


def test_langchain_agent_rejects_non_job_text_import(tmp_path):
    """Agent 工具导入非职位文本时，应返回错误且不写入职位池。"""

    app = JobHuntingApp(tmp_path / "agent.db")
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="离职",
            education="本科",
            experience_years=1,
            skills={"Python": "项目使用"},
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
                        "args": {"raw_text": "今天心情不错，晚上想去吃火锅。"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="这段内容不像招聘职位，我没有保存。"),
        ]
    )

    agent = JobHuntingAgent(app, rag_dir=tmp_path / "chroma", model=model)
    result = agent.chat(
        "帮我导入这段文本。",
        candidate_id=candidate_id,
        session_id="agent-test-invalid-job",
        use_tool_llm=False,
    )

    assert app.list_jobs() == []
    assert result.tool_outputs[0]["data"]["saved"] is False
    assert "不像一段完整的招聘职位信息" in result.tool_outputs[0]["data"]["error"]


def test_langchain_agent_restores_persisted_chat_history_on_startup(tmp_path):
    """新 Agent 进程启动后，会把 SQLite 聊天历史恢复到模型上下文。"""

    app = JobHuntingApp(tmp_path / "agent.db")
    app.initialize()
    candidate_id = app.save_candidate_profile(
        CandidateProfileInput(
            name="小林",
            status="待补充",
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
    session_id = "agent-test-restore"
    app.save_chat_message(candidate_id, session_id, "user", "上一轮我说我会 Python 和 RAG。")
    app.save_chat_message(candidate_id, session_id, "assistant", "已记录你的 Python 和 RAG 经历。")
    model = RecordingToolCallingFakeChatModel(responses=[AIMessage(content="我能看到之前的聊天历史。")])
    agent = JobHuntingAgent(
        app,
        rag_dir=tmp_path / "chroma",
        model=model,
        memory_settings=AgentMemorySettings(summary_trigger_tokens=99999, restore_trigger_tokens=99999),
    )

    result = agent.chat(
        "我刚才说过哪些技能？",
        candidate_id=candidate_id,
        session_id=session_id,
        use_tool_llm=False,
    )
    seen_text = "\n".join(message_text(message) for message in model.seen_messages[0])

    assert result.reply == "我能看到之前的聊天历史。"
    assert "上一轮我说我会 Python 和 RAG" in seen_text
    assert "已记录你的 Python 和 RAG 经历" in seen_text
    assert "我刚才说过哪些技能" in seen_text


def test_langchain_agent_compacts_restored_history_before_model_call(tmp_path):
    """启动恢复的历史过长时，较早消息会压缩成摘要，只保留最近消息原文。"""

    app = JobHuntingApp(tmp_path / "agent.db")
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
    session_id = "agent-test-restore-compact"
    for index in range(6):
        app.save_chat_message(candidate_id, session_id, "user", f"较早资料 {index}：我补充了一段很长的项目背景。")
        app.save_chat_message(candidate_id, session_id, "assistant", f"较早回复 {index}：已整理。")
    app.save_chat_message(candidate_id, session_id, "user", "最近资料：我正在看 AI Agent 实习。")
    app.save_chat_message(candidate_id, session_id, "assistant", "最近回复：建议优先补项目证据。")
    model = RecordingToolCallingFakeChatModel(responses=[AIMessage(content="已基于压缩历史回复。")])
    agent = JobHuntingAgent(
        app,
        rag_dir=tmp_path / "chroma",
        model=model,
        memory_settings=AgentMemorySettings(
            restore_trigger_tokens=1,
            restore_keep_messages=2,
            restore_summary_chars=160,
            summary_trigger_tokens=99999,
        ),
    )

    agent.chat(
        "继续根据历史给建议。",
        candidate_id=candidate_id,
        session_id=session_id,
        use_tool_llm=False,
    )
    seen_text = "\n".join(message_text(message) for message in model.seen_messages[0])

    assert "以下是从 SQLite 持久化聊天历史恢复的压缩上下文" in seen_text
    assert "最近资料：我正在看 AI Agent 实习" in seen_text
    assert "最近回复：建议优先补项目证据" in seen_text
    assert "较早历史已截断" in seen_text


def test_langchain_agent_summarizes_running_context_when_it_gets_too_long(tmp_path):
    """同一进程内对话变长后，LangChain 总结中间件会压缩旧消息再继续回答。"""

    app = JobHuntingApp(tmp_path / "agent.db")
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
    model = RecordingToolCallingFakeChatModel(
        responses=[
            AIMessage(content="第一轮回答。"),
            AIMessage(content="压缩摘要：用户正在构建求职助手，需要保留候选人资料边界。"),
            AIMessage(content="第二轮回答。"),
        ]
    )
    agent = JobHuntingAgent(
        app,
        rag_dir=tmp_path / "chroma",
        model=model,
        memory_settings=AgentMemorySettings(
            restore_trigger_tokens=99999,
            summary_trigger_tokens=1,
            summary_keep_messages=1,
            summary_trim_tokens=1000,
        ),
    )

    agent.chat("第一轮：我在做一个求职助手 Agent。", candidate_id, session_id="agent-test-summary")
    result = agent.chat("第二轮：继续。", candidate_id, session_id="agent-test-summary")
    final_seen_text = "\n".join(message_text(message) for message in model.seen_messages[-1])

    assert result.reply == "第二轮回答。"
    assert len(model.seen_messages) == 3
    assert "压缩摘要：用户正在构建求职助手" in final_seen_text
    assert "第二轮：继续" in final_seen_text


def message_text(message: BaseMessage) -> str:
    """把测试里的 LangChain 消息压平成字符串，便于断言。"""

    content = message.content
    if isinstance(content, str):
        return content
    return str(content)
