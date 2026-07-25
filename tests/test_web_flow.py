"""Web 前端/API 行为测试。

网页入口是给不熟悉 CLI 的使用者准备的本地界面。测试重点不放在像素级样式，
而是验证页面资源可访问、Web API 会调用现有 `JobHuntingApp`，并保持 SQLite/RAG
边界不变。
"""

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from job_hunting_agent.agent import JobHuntingAgent
from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.web import create_web_app


class ToolCallingFakeChatModel(FakeMessagesListChatModel):
    """测试用假模型：支持 `create_agent` 的工具绑定。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # noqa: ANN001,D401
        """直接返回自身，让测试可以手工指定工具调用序列。"""

        return self


def test_web_home_page_and_assets_are_available(tmp_path):
    """本地 Web 应用可以打开首页，并加载前端静态资源。"""

    client = TestClient(create_web_app(db_path=tmp_path / "web.db", rag_dir=tmp_path / "chroma"))

    home = client.get("/")
    script = client.get("/static/app.js")
    styles = client.get("/static/styles.css")

    assert home.status_code == 200
    assert "Job Hunting Agent" in home.text
    assert script.status_code == 200
    assert styles.status_code == 200


def test_web_can_create_profile_and_ingest_chat_message_incrementally(tmp_path):
    """网页 API 可以创建候选人档案，并通过聊天消息自动入库和增量索引。"""

    client = TestClient(create_web_app(db_path=tmp_path / "web.db", rag_dir=tmp_path / "chroma"))
    created = client.post(
        "/api/profiles",
        json={
            "name": "小林",
            "status": "待补充",
            "education": "大专",
            "experience_years": 0,
            "skills": {},
            "preferred_cities": [],
            "salary_floor_k": None,
            "expected_salary_k": None,
            "target_directions": [],
            "unacceptable": [],
        },
    )
    candidate_id = created.json()["candidate_id"]

    chat = client.post(
        "/api/chat",
        json={
            "candidate_id": candidate_id,
            "message": "我是本科，1年经验，会 Python 和 FastAPI。做过一个求职助手项目。",
            "auto_rag": True,
            "use_env_llm": False,
        },
    )
    profile = client.get(f"/api/profiles/{candidate_id}").json()["profile"]
    rag = client.get("/api/rag/search", params={"query": "FastAPI 求职助手"}).json()

    assert chat.status_code == 200
    assert chat.json()["result"]["rag_update_mode"] == "incremental"
    assert profile["education"] == "本科"
    assert profile["skills"]["Python"] == "待确认"
    assert any("FastAPI" in item["content"] for item in rag["results"])


def test_web_can_import_job_and_return_matches(tmp_path):
    """网页 API 可以导入候选人复制回来的 BOSS 职位文本，并返回匹配结果。"""

    client = TestClient(create_web_app(db_path=tmp_path / "web.db", rag_dir=tmp_path / "chroma"))
    candidate_id = client.post(
        "/api/profiles",
        json={
            "name": "小林",
            "status": "离职",
            "education": "本科",
            "experience_years": 1,
            "skills": {"Python": "项目使用", "FastAPI": "项目使用"},
            "preferred_cities": ["杭州"],
            "salary_floor_k": 10,
            "expected_salary_k": 15,
            "target_directions": ["Python 后端开发"],
            "unacceptable": [],
        },
    ).json()["candidate_id"]
    imported = client.post(
        "/api/jobs",
        json={
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
    ).json()["job"]
    matches = client.get(f"/api/matches/{candidate_id}").json()["matches"]

    assert imported["title"] == "Python 后端开发工程师"
    assert matches
    assert matches[0]["job"]["id"] == imported["id"]
    assert matches[0]["match"]["tier"] in {"强推荐", "可投递"}


def test_web_chat_can_use_langchain_agent_mode(tmp_path):
    """网页聊天在开启开关时，会走标准 LangChain Agent 主流程。"""

    agent_backend = JobHuntingApp(tmp_path / "web.db")
    agent_backend.initialize()
    model = ToolCallingFakeChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "ingest_candidate_message",
                        "args": {
                            "message": "我是本科，1年经验，会 Python 和 FastAPI。",
                            "auto_rag": True,
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="我已经通过 Agent 工具保存了你的资料。"),
        ]
    )
    backend_app = create_web_app(
        db_path=tmp_path / "web.db",
        rag_dir=tmp_path / "chroma",
        chat_agent=JobHuntingAgent(
            app=agent_backend,
            rag_dir=tmp_path / "chroma",
            model=model,
        ),
    )
    client = TestClient(backend_app)
    created = client.post(
        "/api/profiles",
        json={
            "name": "小林",
            "status": "待补充",
            "education": "大专",
            "experience_years": 0,
            "skills": {},
            "preferred_cities": [],
            "salary_floor_k": None,
            "expected_salary_k": None,
            "target_directions": [],
            "unacceptable": [],
        },
    )
    candidate_id = created.json()["candidate_id"]

    chat = client.post(
        "/api/chat",
        json={
            "candidate_id": candidate_id,
            "message": "我是本科，1年经验，会 Python 和 FastAPI。",
            "auto_rag": True,
            "use_env_llm": True,
        },
    )

    assert chat.status_code == 200
    assert chat.json()["mode"] == "langchain_agent"
    assert "ingest_candidate_message" in chat.json()["used_tools"]
