"""Web 前端/API 行为测试。

网页入口是给不熟悉 CLI 的使用者准备的本地界面。测试重点不放在像素级样式，
而是验证页面资源可访问、Web API 会调用现有 `JobHuntingApp`，并保持 SQLite/RAG
边界不变。
"""

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeListChatModel, FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from job_hunting_agent.agent import JobHuntingAgent
from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import CandidateProfileInput
from job_hunting_agent.web import ChatPayload, create_web_app


class ToolCallingFakeChatModel(FakeMessagesListChatModel):
    """测试用假模型：支持 `create_agent` 的工具绑定。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # noqa: ANN001,D401
        """直接返回自身，让测试可以手工指定工具调用序列。"""

        return self


class StreamingFakeChatModel(FakeListChatModel):
    """测试用流式假模型：用于验证 Web SSE 不会退化成单次完整输出。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # noqa: ANN001,D401
        """直接返回自身，让 `create_agent` 保留 fake 模型的 `_stream` 行为。"""

        return self


def legacy_client(db_path, rag_dir):
    """旧版 Web 行为测试显式关闭认证，生产默认仍要求登录。"""

    return TestClient(create_web_app(db_path=db_path, rag_dir=rag_dir, require_auth=False))


def test_web_chat_payload_defaults_to_langchain_agent() -> None:
    """省略旧开关字段时，后端也必须默认走 LangChain Agent 主流程。"""

    payload = ChatPayload(candidate_id=1, message="你好")

    assert payload.use_env_llm is True
    assert payload.auto_rag is True


def test_secure_cookie_setting_is_loaded_from_project_env(tmp_path, monkeypatch) -> None:
    """Cookie 安全开关应读取项目 `.env`，而不要求用户额外导出系统变量。"""

    monkeypatch.delenv("JOB_AGENT_COOKIE_SECURE", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("JOB_AGENT_COOKIE_SECURE=true\n", encoding="utf-8")
    client = TestClient(
        create_web_app(
            db_path=tmp_path / "web.db",
            env_file=env_path,
            rag_dir=tmp_path / "chroma",
            require_auth=True,
        )
    )
    registered = client.post(
        "/api/auth/register",
        json={"email": "secure@example.com", "password": "password-123"},
    )
    response = client.post(
        "/api/auth/login",
        json={"email": "secure@example.com", "password": "password-123"},
    )

    assert registered.status_code == 200
    assert response.status_code == 200
    assert "; secure" in response.headers["set-cookie"].lower()


def test_web_home_page_and_assets_are_available(tmp_path):
    """本地 Web 应用可以打开首页，并加载前端静态资源。"""

    client = legacy_client(tmp_path / "web.db", tmp_path / "chroma")

    home = client.get("/")
    script = client.get("/static/app.js")
    styles = client.get("/static/styles.css")
    tokens = client.get("/static/tokens.css")
    vue = client.get("/static/vendor/vue.global.prod.js")

    assert home.status_code == 200
    assert "Job Hunting Agent" in home.text
    assert '/static/app.js?v=20260731-auth-admin' in home.text
    assert '/static/styles.css?v=20260731-auth-admin' in home.text
    assert "本地运行 · 用户复制职位文本" not in home.text
    assert "Conversation Workspace" not in home.text
    assert "整理求职证据" not in home.text
    assert "status-pill" not in home.text
    assert "mini-metrics" not in home.text
    assert "自动增量 RAG" not in home.text
    assert "使用 LangChain Agent（需 .env）" not in home.text
    assert home.headers["cache-control"] == "no-store, max-age=0"
    assert script.status_code == 200
    assert script.headers["cache-control"] == "no-store, max-age=0"
    assert styles.status_code == 200
    assert styles.headers["cache-control"] == "no-store, max-age=0"
    assert tokens.status_code == 200
    assert "Hallmark · tokens" in tokens.text
    assert "oklch(" in tokens.text
    assert vue.status_code == 200
    assert "Vue" in vue.text


def test_web_health_reports_enabled_memory_as_configured(tmp_path):
    """记忆配置成功且启用时，健康接口不能错误显示为未配置。"""

    client = legacy_client(tmp_path / "web.db", tmp_path / "chroma")

    health = client.get("/api/health")

    assert health.status_code == 200
    assert health.json()["memory"]["configured"] is True


def test_web_auth_bootstrap_does_not_surface_probe_errors_in_login_form(tmp_path):
    """初始化 Session 探测失败时，不应把错误提前显示成登录失败。"""

    client = legacy_client(tmp_path / "web.db", tmp_path / "chroma")
    script = client.get("/static/app.js").text
    check_auth_start = script.index("async checkAuth()")
    check_auth_end = script.index("/** 切换登录与注册表单。 */", check_auth_start)
    check_auth_body = script[check_auth_start:check_auth_end]

    assert "this.authError =" not in check_auth_body


def test_web_frontend_defaults_to_agent_and_incremental_rag_without_toggles(tmp_path):
    """网页聊天不再暴露模式开关，而是固定走 Agent + 自动增量 RAG。"""

    client = legacy_client(tmp_path / "web.db", tmp_path / "chroma")

    home = client.get("/").text
    script = client.get("/static/app.js").text

    assert 'v-model="autoRag"' not in home
    assert 'v-model="useLlm"' not in home
    assert "DEFAULT_USE_LANGCHAIN_AGENT = true" in script
    assert "DEFAULT_AUTO_INCREMENTAL_RAG = true" in script
    assert "use_env_llm: DEFAULT_USE_LANGCHAIN_AGENT" in script
    assert "auto_rag: DEFAULT_AUTO_INCREMENTAL_RAG" in script
    assert "this.useLlm =" not in script


def test_web_profile_form_uses_recovered_selectors_and_auth_copy(tmp_path):
    """恢复注册密码显示、学历枚举和单框省市选择等前端约束。"""

    client = legacy_client(tmp_path / "web.db", tmp_path / "chroma")

    home = client.get("/").text
    script = client.get("/static/app.js").text
    cities = client.get("/static/china_cities.js")

    assert ':type="authMode === \'register\' ? \'text\' : \'password\'"' in home
    assert 'placeholder="例如：小林"' not in home
    assert 'placeholder="Python=项目使用,FastAPI=待确认"' not in home
    assert 'placeholder="AI Agent 应用开发"' not in home
    assert "Local Boundary" not in home
    assert "运行边界" not in home
    assert home.count("退出所有设备") == 1
    for education in ("高中及以下", "大专", "本科", "硕士", "博士"):
        assert f'<option value="{education}">{education}</option>' in home
    assert 'v-for="province in cityGroups"' in home
    assert 'v-for="city in province.cities"' in home
    assert '/static/china_cities.js?v=20260803-cities' in home
    assert "cityGroups: buildSortedCityGroups()" in script
    assert "preferred_cities: this.profileForm.city ? [this.profileForm.city] : []" in script
    assert cities.status_code == 200
    assert "北京市" in cities.text
    assert "广州市" in cities.text
    assert "乌鲁木齐市" in cities.text


def test_web_can_create_profile_and_ingest_chat_message_incrementally(tmp_path):
    """网页 API 可以创建候选人档案，并通过聊天消息自动入库和增量索引。"""

    client = legacy_client(tmp_path / "web.db", tmp_path / "chroma")
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

    assert chat.status_code == 200, chat.text
    assert chat.json()["result"]["rag_update_mode"] == "incremental"
    assert profile["education"] == "本科"
    assert profile["skills"]["Python"] == "待确认"
    assert any("FastAPI" in item["content"] for item in rag["results"])


def test_web_can_import_job_and_return_matches(tmp_path):
    """网页 API 可以导入候选人复制回来的 BOSS 职位文本，并返回匹配结果。"""

    client = legacy_client(tmp_path / "web.db", tmp_path / "chroma")
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


def test_web_rejects_non_job_text_before_saving(tmp_path):
    """导入职位前应审核文本；非招聘职位内容不能进入职位池。"""

    client = legacy_client(tmp_path / "web.db", tmp_path / "chroma")

    response = client.post(
        "/api/jobs",
        json={"raw_text": "今天心情不错，晚上想去吃火锅。"},
    )
    jobs_after_plain_text = client.get("/api/jobs").json()["jobs"]

    assert response.status_code == 400
    assert "不像一段完整的招聘职位信息" in response.json()["detail"]
    assert jobs_after_plain_text == []


def test_web_rejects_project_changelog_as_job_text(tmp_path):
    """项目更新日志包含技术词，也不能被误当成职位信息保存和打分。"""

    client = legacy_client(tmp_path / "web.db", tmp_path / "chroma")

    response = client.post(
        "/api/jobs",
        json={
            "raw_text": """
            - 新增 Java AiGateway 模块，统一封装 Spring Boot 到 Python AI 服务的导入。
            - 新增知识库异步导入记录，保存解析、Embedding、索引任务状态。
            - 用户端 AI 会话持久化，刷新页面后可以恢复历史消息。
            """,
        },
    )

    assert response.status_code == 400
    assert client.get("/api/jobs").json()["jobs"] == []


def test_web_hides_legacy_invalid_job_rows_from_listing_and_matching(tmp_path):
    """历史误入库的非职位记录不应继续出现在前端列表或匹配结果里。"""

    db_path = tmp_path / "web.db"
    backend = JobHuntingApp(db_path)
    backend.initialize()
    candidate_id = backend.save_candidate_profile(
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

    with backend.store.connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                raw_text, source_url, title, city, salary_min_k, salary_max_k,
                salary_months, salary_unit, experience_min_years,
                experience_max_years, experience_label, education,
                company_name, industry, company_size, skills_json,
                description_text, field_confidence_json, uncertainty_notes_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "今天心情不错，晚上想去吃火锅。",
                None,
                "今天心情不错，晚上想去吃火锅。",
                None,
                None,
                None,
                None,
                "unknown",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                "[]",
                "今天心情不错，晚上想去吃火锅。",
                "{}",
                "[]",
            ),
        )

    client = legacy_client(db_path, tmp_path / "chroma")

    assert client.get("/api/jobs").json()["jobs"] == []
    assert client.get(f"/api/matches/{candidate_id}").json()["matches"] == []


def test_web_frontend_loads_persisted_jobs_on_page_open(tmp_path):
    """页面脚本应在打开时主动拉取已导入职位，而不是只在匹配接口里临时使用。"""

    client = legacy_client(tmp_path / "web.db", tmp_path / "chroma")
    client.post(
        "/api/jobs",
        json={
            "raw_text": """
            Java AI Gateway 工程师
            20-30K
            上海
            3-5年
            本科
            职位描述：负责 Spring Boot 到 Python AI 服务的接入。
            """,
        },
    )

    jobs = client.get("/api/jobs").json()["jobs"]
    script = client.get("/static/app.js").text
    home = client.get("/").text

    assert jobs[0]["title"] == "Java AI Gateway 工程师"
    assert 'id="jobList"' in home
    assert "async loadJobs()" in script
    assert "await this.loadJobs();" in script
    assert "jobImportError" in script


def test_web_chat_history_survives_page_reopen(tmp_path):
    """网页聊天记录应保存到 SQLite，刷新或重新打开页面后可以恢复。"""

    db_path = tmp_path / "web.db"
    client = legacy_client(db_path, tmp_path / "chroma")
    candidate_id = client.post(
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
    ).json()["candidate_id"]

    chat = client.post(
        "/api/chat",
        json={
            "candidate_id": candidate_id,
            "message": "我是本科，1年经验，会 Python。",
            "auto_rag": False,
            "use_env_llm": False,
            "session_id": f"web-candidate-{candidate_id}",
        },
    )
    reopened_client = legacy_client(db_path, tmp_path / "chroma")
    history = reopened_client.get(
        "/api/chat/history",
        params={"candidate_id": candidate_id, "session_id": f"web-candidate-{candidate_id}"},
    )

    assert chat.status_code == 200
    assert history.status_code == 200
    messages = history.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "我是本科，1年经验，会 Python。"
    assert "保存字段" in messages[1]["content"]


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
        require_auth=False,
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

    assert chat.status_code == 200, chat.text
    assert chat.json()["mode"] == "langchain_agent"
    assert "ingest_candidate_message" in chat.json()["used_tools"]


def test_web_chat_stream_returns_sse_and_saves_history(tmp_path):
    """网页流式聊天接口会返回 SSE 事件，并在 final 后保存聊天历史。"""

    agent_backend = JobHuntingApp(tmp_path / "web.db")
    agent_backend.initialize()
    model = ToolCallingFakeChatModel(responses=[AIMessage(content="这是流式回复。")])
    backend_app = create_web_app(
        db_path=tmp_path / "web.db",
        rag_dir=tmp_path / "chroma",
        require_auth=False,
        chat_agent=JobHuntingAgent(
            app=agent_backend,
            rag_dir=tmp_path / "chroma",
            model=model,
        ),
    )
    client = TestClient(backend_app)
    candidate_id = client.post(
        "/api/profiles",
        json={
            "name": "小林",
            "status": "待补充",
            "education": "本科",
            "experience_years": 1,
            "skills": {},
            "preferred_cities": [],
            "salary_floor_k": None,
            "expected_salary_k": None,
            "target_directions": [],
            "unacceptable": [],
        },
    ).json()["candidate_id"]

    response = client.post(
        "/api/chat/stream",
        json={
            "candidate_id": candidate_id,
            "message": "请用流式回复。",
            "auto_rag": False,
            "use_env_llm": True,
            "session_id": f"web-candidate-{candidate_id}",
        },
    )
    history = client.get(
        "/api/chat/history",
        params={"candidate_id": candidate_id, "session_id": f"web-candidate-{candidate_id}"},
    ).json()["messages"]

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: token" in response.text
    assert "event: final" in response.text
    assert "这是流式回复。" in response.text
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[1]["content"].startswith("这是流式回复。")


def test_web_chat_stream_preserves_multiple_token_events(tmp_path):
    """底层模型支持 token stream 时，Web SSE 也必须向前端转发多个 token。"""

    agent_backend = JobHuntingApp(tmp_path / "web.db")
    agent_backend.initialize()
    model = StreamingFakeChatModel(responses=["流式OK"])
    backend_app = create_web_app(
        db_path=tmp_path / "web.db",
        rag_dir=tmp_path / "chroma",
        require_auth=False,
        chat_agent=JobHuntingAgent(
            app=agent_backend,
            rag_dir=tmp_path / "chroma",
            model=model,
        ),
    )
    client = TestClient(backend_app)
    candidate_id = client.post(
        "/api/profiles",
        json={
            "name": "小林",
            "status": "待补充",
            "education": "本科",
            "experience_years": 1,
            "skills": {},
            "preferred_cities": [],
            "salary_floor_k": None,
            "expected_salary_k": None,
            "target_directions": [],
            "unacceptable": [],
        },
    ).json()["candidate_id"]

    response = client.post(
        "/api/chat/stream",
        json={
            "candidate_id": candidate_id,
            "message": "请用多个 token 流式回复。",
            "auto_rag": False,
            "use_env_llm": True,
            "session_id": f"web-candidate-{candidate_id}",
        },
    )

    assert response.status_code == 200
    assert response.text.count("event: token") == 4
    assert '{"content": "流"}' in response.text
    assert '{"content": "式"}' in response.text
    assert '{"content": "O"}' in response.text
    assert '{"content": "K"}' in response.text


def test_web_chat_bubble_uses_markdown_renderer(tmp_path):
    """Vue 聊天气泡应渲染 Markdown，而不是把模型回复按纯文本展示。"""

    client = legacy_client(tmp_path / "web.db", tmp_path / "chroma")
    script = client.get("/static/app.js").text
    home = client.get("/").text

    # 这个测试锁住用户报告的具体问题：Vue 模板必须通过 v-html 使用安全 Markdown 渲染器，
    # 否则 **加粗**、列表和代码块都会在页面上原样显示。
    assert 'id="app"' in home
    assert "/static/vendor/vue.global.prod.js?v=20260731-auth-admin" in home
    assert 'v-html="renderMarkdown(message.content)"' in home
    assert "Vue.createApp" in script or "createApp({" in script
    assert "renderMarkdown(text)" in script
    assert '"/api/chat/stream"' in script
    assert "response.body.getReader()" in script
    assert "splitStreamDisplayChunks(content)" in script
    assert "requestAnimationFrame" in script
    assert "v-cloak" in home


def test_web_stream_message_keeps_vue_reactive_proxy(tmp_path):
    """流式更新必须持有 Vue 数组里的 Proxy，不能继续修改 push 前的原始对象。"""

    client = legacy_client(tmp_path / "web.db", tmp_path / "chroma")

    script = client.get("/static/app.js").text

    # Vue 3 不会追踪通过原始对象引用执行的属性修改。push 后重新从响应式数组读取，
    # 才能保证每个 token 都触发气泡重绘，而不是等其他状态变化后一次性显示全文。
    assert "const reactiveIndex = this.messages.push(message) - 1;" in script
    assert "return this.messages[reactiveIndex];" in script


def test_web_markdown_renderer_supports_tables(tmp_path):
    """模型输出标准 Markdown 表格时，前端必须生成表格 DOM 和响应式滚动容器。"""

    client = legacy_client(tmp_path / "web.db", tmp_path / "chroma")

    script = client.get("/static/app.js").text
    styles = client.get("/static/styles.css").text

    # 锁住用户报告的具体格式：表头行后紧跟 `|---|---|` 分隔行时，不能再落入普通段落。
    assert "isMarkdownTableStart(lines, index)" in script
    assert "renderMarkdownTable(lines, index)" in script
    assert '<div class="markdown-table-wrap"><table>' in script
    assert "<thead>" in script
    assert "<tbody>" in script
    assert ".markdown-table-wrap" in styles
    assert ".bubble table" in styles
