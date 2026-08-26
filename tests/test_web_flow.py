"""Web 前端/API 行为测试。

网页入口是面向日常使用者的本地界面。测试重点不放在像素级样式，
而是验证页面资源可访问、Web API 会调用现有 `JobHuntingApp`，并保持 PostgreSQL/pgvector
边界不变。
"""

import json
import logging
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage

from job_hunting_agent.agent import JobHuntingAgent
from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.models import (
    CandidateProfileInput,
    CandidateProfilePatch,
    ProjectExperienceCard,
    UsageEventRecord,
)
from job_hunting_agent.web import (
    ChatPayload,
    create_web_app,
    format_web_chat_reply,
    sanitize_web_chat_reply,
)


class ToolCallingFakeChatModel(FakeMessagesListChatModel):
    """测试用假模型：支持 `create_agent` 的工具绑定。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """直接返回自身，让测试可以手工指定工具调用序列。"""

        return self


class StreamingFakeChatModel(FakeListChatModel):
    """测试用流式假模型：用于验证 Web SSE 不会退化成单次完整输出。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """直接返回自身，让 `create_agent` 保留 fake 模型的 `_stream` 行为。"""

        return self


class RecordingAccountEmailSender:
    """记录账号邮件链接，不依赖真实 SMTP。"""

    def __init__(self) -> None:
        self.verification_messages: list[tuple[str, str]] = []
        self.password_reset_messages: list[tuple[str, str]] = []

    def send_verification(self, email: str, action_url: str) -> None:
        self.verification_messages.append((email, action_url))

    def send_password_reset(self, email: str, action_url: str) -> None:
        self.password_reset_messages.append((email, action_url))


def login_test_account(
    client: TestClient,
    email: str = "web-tests@example.com",
    password: str = "password-123",
) -> TestClient:
    """为 Web 行为测试创建真实账号并建立 HttpOnly Session。"""

    registered = client.post(
        "/api/auth/register",
        json={"email": email, "password": password},
    )
    assert registered.status_code in {200, 409}
    logged_in = client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
    )
    assert logged_in.status_code == 200
    return client


def legacy_client(*_unused):
    """创建已登录客户端，保留原测试调用名称以避免无关改写。"""

    return login_test_account(TestClient(create_web_app()))


def test_web_registration_does_not_persist_password_as_display_name():
    """Web 注册也不能把密码误写入账号显示名。"""

    client = TestClient(create_web_app())
    response = client.post(
        "/api/auth/register",
        json={
            "email": "display-name-web-guard@example.com",
            "password": "password-123",
            "display_name": "password-123",
        },
    )

    assert response.status_code == 200
    account = response.json()["account"]
    assert account["email"] == "display-name-web-guard@example.com"
    assert account["display_name"] is None
    assert "password_hash" not in account

    logged_in = client.post(
        "/api/auth/login",
        json={
            "email": "display-name-web-guard@example.com",
            "password": "password-123",
        },
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["account"]["email"] == "display-name-web-guard@example.com"


def test_email_verification_blocks_login_until_one_time_link_is_consumed(tmp_path) -> None:
    """需要邮箱验证时，未验证账号不能登录，验证令牌只能使用一次。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            (
                "JOB_AGENT_EMAIL_VERIFICATION_REQUIRED=true",
                "JOB_AGENT_ACCOUNT_EMAIL_BACKEND=console",
                "JOB_AGENT_PUBLIC_BASE_URL=https://agent.example.com",
                "JOB_AGENT_CONSENT_REQUIRED=true",
                "JOB_AGENT_TERMS_VERSION=2026-08-26",
                "JOB_AGENT_PRIVACY_VERSION=2026-08-26",
            )
        ),
        encoding="utf-8",
    )
    sender = RecordingAccountEmailSender()
    client = TestClient(create_web_app(env_file=env_path, account_email_sender=sender))

    registered = client.post(
        "/api/auth/register",
        json={
            "email": "verify-me@example.com",
            "password": "password-123",
            "accepted_terms_version": "2026-08-26",
            "accepted_privacy_version": "2026-08-26",
        },
    )

    assert registered.status_code == 200
    assert registered.json()["verification_required"] is True
    assert registered.json()["account"]["email_verified_at"] is None
    assert sender.verification_messages[0][0] == "verify-me@example.com"
    token = parse_qs(urlsplit(sender.verification_messages[0][1]).query)[
        "verify_email_token"
    ][0]
    assert client.post(
        "/api/auth/login",
        json={"email": "verify-me@example.com", "password": "password-123"},
    ).status_code == 403

    verified = client.post("/api/auth/verify-email", json={"token": token})

    assert verified.status_code == 200
    assert verified.json()["account"]["email_verified_at"]
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 400
    assert client.post(
        "/api/auth/login",
        json={"email": "verify-me@example.com", "password": "password-123"},
    ).status_code == 200


def test_password_reset_is_one_time_and_revokes_existing_sessions(tmp_path) -> None:
    """重置密码不泄露账号存在性，并使旧密码、旧 Session 和令牌全部失效。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "JOB_AGENT_ACCOUNT_EMAIL_BACKEND=console\n"
        "JOB_AGENT_PUBLIC_BASE_URL=https://agent.example.com\n",
        encoding="utf-8",
    )
    sender = RecordingAccountEmailSender()
    client = TestClient(create_web_app(env_file=env_path, account_email_sender=sender))
    email = "reset-me@example.com"
    old_password = "password-123"
    new_password = "changed-password-456"
    assert client.post(
        "/api/auth/register",
        json={"email": email, "password": old_password},
    ).status_code == 200
    assert client.post(
        "/api/auth/login",
        json={"email": email, "password": old_password},
    ).status_code == 200

    unknown = client.post(
        "/api/auth/password-reset/request",
        json={"email": "unknown@example.com"},
    )
    requested = client.post(
        "/api/auth/password-reset/request",
        json={"email": email},
    )

    assert unknown.status_code == requested.status_code == 200
    assert unknown.json() == requested.json()
    assert len(sender.password_reset_messages) == 1
    token = parse_qs(urlsplit(sender.password_reset_messages[0][1]).query)[
        "reset_password_token"
    ][0]
    reset = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "new_password": new_password},
    )

    assert reset.status_code == 200
    assert client.get("/api/auth/me").json()["authenticated"] is False
    assert client.post(
        "/api/auth/login",
        json={"email": email, "password": old_password},
    ).status_code == 401
    assert client.post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "new_password": new_password},
    ).status_code == 400
    assert client.post(
        "/api/auth/login",
        json={"email": email, "password": new_password},
    ).status_code == 200


def test_account_export_change_password_and_anonymized_deletion() -> None:
    """用户能导出数据；注销清理求职数据，但保留匿名财务事实。"""

    web_app = create_web_app()
    client = TestClient(web_app)
    email = "lifecycle-rights@example.com"
    old_password = "password-123"
    new_password = "changed-password-456"
    registered = client.post(
        "/api/auth/register",
        json={"email": email, "password": old_password},
    )
    account_id = registered.json()["account"]["id"]
    login = client.post(
        "/api/auth/login",
        json={"email": email, "password": old_password},
    )
    csrf_headers = {"X-CSRF-Token": login.json()["csrf_token"]}
    assert client.post(
        "/api/profiles",
        headers=csrf_headers,
        json={"name": "待注销候选人"},
    ).status_code == 200

    exported = client.get("/api/account/export")

    assert exported.status_code == 200
    assert "attachment" in exported.headers["content-disposition"]
    export_text = exported.text.lower()
    assert email in export_text
    assert "password_hash" not in export_text
    assert "token_hash" not in export_text

    changed = client.post(
        "/api/account/password",
        headers=csrf_headers,
        json={"current_password": old_password, "new_password": new_password},
    )
    assert changed.status_code == 200
    assert client.get("/api/auth/me").json()["authenticated"] is False
    relogin = client.post(
        "/api/auth/login",
        json={"email": email, "password": new_password},
    )
    delete_headers = {"X-CSRF-Token": relogin.json()["csrf_token"]}

    deleted = client.post(
        "/api/account/delete",
        headers=delete_headers,
        json={"current_password": new_password, "confirmation": "注销账号"},
    )

    assert deleted.status_code == 200
    assert client.get("/api/auth/me").json()["authenticated"] is False
    store = web_app.state.backend.store
    anonymized = store.get_account(account_id)
    assert anonymized.deleted_at is not None
    assert anonymized.status == "disabled"
    assert anonymized.email != email
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM candidate_profiles WHERE account_id = ?",
            (account_id,),
        ).fetchone()["count"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM account_balances WHERE account_id = ?",
            (account_id,),
        ).fetchone()["count"] == 1


def test_account_deletion_restores_login_when_object_cleanup_fails(monkeypatch) -> None:
    """对象清理失败时不能把尚未注销的用户永久锁死。"""

    web_app = create_web_app()
    client = TestClient(web_app)
    email = "deletion-retry@example.com"
    password = "password-123"
    account_id = client.post(
        "/api/auth/register",
        json={"email": email, "password": password},
    ).json()["account"]["id"]
    login = client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
    )
    store = web_app.state.backend.store
    original_prepare = store.prepare_account_deletion
    monkeypatch.setattr(
        store,
        "prepare_account_deletion",
        lambda target_id: original_prepare(target_id) + ["accounts/test/failing-object"],
    )

    class FailingObjectStorage:
        def delete(self, storage_key: str) -> None:
            raise RuntimeError("simulated object storage outage")

    web_app.state.backend.resume_files = FailingObjectStorage()
    response = client.post(
        "/api/account/delete",
        headers={"X-CSRF-Token": login.json()["csrf_token"]},
        json={"current_password": password, "confirmation": "注销账号"},
    )

    assert response.status_code == 503
    account = store.get_account(account_id)
    assert account.status == "active"
    assert account.deleted_at is None
    assert client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
    ).status_code == 200


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
            env_file=env_path,
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


@pytest.mark.parametrize(
    "disabled_setting",
    [
        "JOB_AGENT_COOKIE_SECURE",
        "JOB_AGENT_CSRF_ENABLED",
        "JOB_AGENT_SECURITY_HEADERS_ENABLED",
        "JOB_AGENT_RATE_LIMIT_ENABLED",
    ],
)
def test_production_rejects_disabled_web_security_controls(
    tmp_path,
    monkeypatch,
    disabled_setting,
) -> None:
    """生产环境不能通过配置关闭已承诺的 Web 安全边界。"""

    settings = {
        "JOB_AGENT_ENVIRONMENT": "production",
        "JOB_AGENT_COOKIE_SECURE": "true",
        "JOB_AGENT_CSRF_ENABLED": "true",
        "JOB_AGENT_SECURITY_HEADERS_ENABLED": "true",
        "JOB_AGENT_RATE_LIMIT_ENABLED": "true",
        "JOB_AGENT_CONCURRENCY_BACKEND": "redis",
        "JOB_AGENT_CONCURRENCY_REDIS_URL": "redis://redis:6379/1",
    }
    settings[disabled_setting] = "false"
    for key in settings:
        monkeypatch.delenv(key, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(f"{key}={value}" for key, value in settings.items()),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="生产环境"):
        create_web_app(env_file=env_path)


def test_web_hardening_adds_request_id_security_headers_and_access_log(caplog) -> None:
    """所有 Web 响应都应带请求 ID、安全响应头，并输出不含正文的结构化访问日志。"""

    caplog.set_level(logging.INFO, logger="job_hunting_agent.web.access")
    client = TestClient(create_web_app())

    response = client.get("/", headers={"X-Request-ID": "request-test-123"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "request-test-123"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "script-src 'self' 'unsafe-eval'" in response.headers["content-security-policy"]
    records = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "job_hunting_agent.web.access"
    ]
    assert {
        "event": "http_request",
        "request_id": "request-test-123",
        "method": "GET",
        "path": "/",
        "status_code": 200,
    }.items() <= records[-1].items()
    assert "body" not in records[-1]


def test_web_hardening_keeps_request_id_and_security_headers_on_unhandled_500() -> None:
    """未处理异常生成的 500 响应也必须保留统一追踪与安全响应头。"""

    app = create_web_app()

    @app.get("/api/test/unhandled-error")
    def unhandled_error():
        raise RuntimeError("test failure")

    response = TestClient(app, raise_server_exceptions=False).get(
        "/api/test/unhandled-error",
        headers={"X-Request-ID": "request-error-123"},
    )

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "request-error-123"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_csrf_header_is_required_for_authenticated_mutations(monkeypatch) -> None:
    """启用 CSRF 后，已登录浏览器的状态变更请求必须带双提交 token。"""

    monkeypatch.setenv("JOB_AGENT_CSRF_ENABLED", "true")
    client = TestClient(create_web_app())
    assert client.post(
        "/api/auth/register",
        json={"email": "csrf@example.com", "password": "password-123"},
    ).status_code == 200
    login = client.post(
        "/api/auth/login",
        json={"email": "csrf@example.com", "password": "password-123"},
    )
    assert login.status_code == 200
    csrf_token = login.json()["csrf_token"]

    blocked = client.post(
        "/api/profiles",
        json={"name": "CSRF 测试候选人"},
    )
    allowed = client.post(
        "/api/profiles",
        headers={"X-CSRF-Token": csrf_token},
        json={"name": "CSRF 测试候选人"},
    )

    assert blocked.status_code == 403
    assert "CSRF" in blocked.json()["detail"]
    assert allowed.status_code == 200


def test_auth_rate_limit_rejects_excessive_login_attempts(monkeypatch) -> None:
    """认证入口应有独立低阈值限流，避免暴力尝试密码。"""

    monkeypatch.setenv("JOB_AGENT_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("JOB_AGENT_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("JOB_AGENT_RATE_LIMIT_AUTH_REQUESTS", "2")
    client = TestClient(create_web_app())

    first = client.post(
        "/api/auth/login",
        json={"email": "nobody@example.com", "password": "bad-password"},
    )
    second = client.post(
        "/api/auth/login",
        json={"email": "nobody@example.com", "password": "bad-password"},
    )
    third = client.post(
        "/api/auth/login",
        json={"email": "nobody@example.com", "password": "bad-password"},
    )

    assert first.status_code == 401
    assert second.status_code == 401
    assert third.status_code == 429
    assert int(third.headers["retry-after"]) > 0


def test_admin_can_read_low_cardinality_request_metrics(tmp_path) -> None:
    """管理员能读取当前 Web 进程请求指标，指标不保存查询参数或正文。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "JOB_AGENT_BOOTSTRAP_ADMIN_EMAIL=metrics-admin@example.com",
                "JOB_AGENT_BOOTSTRAP_ADMIN_PASSWORD=strong-password-123",
            ]
        ),
        encoding="utf-8",
    )
    client = TestClient(create_web_app(env_file=env_path))
    assert client.post(
        "/api/auth/login",
        json={"email": "metrics-admin@example.com", "password": "strong-password-123"},
    ).status_code == 200
    assert client.get("/api/missing?secret=should-not-appear").status_code == 404

    response = client.get("/api/admin/observability/requests")

    assert response.status_code == 200
    metrics = response.json()["requests"]
    assert metrics["total_requests"] >= 2
    assert metrics["error_requests"] >= 1
    assert metrics["average_duration_ms"] >= 0
    assert "/api/auth/login" in metrics["endpoint_counts"]
    latest_error = metrics["recent_errors"][0]
    assert latest_error["endpoint"] == "/api/missing"
    assert "secret" not in str(metrics)

    prometheus = client.get("/internal/metrics")
    assert prometheus.status_code == 200
    assert prometheus.headers["content-type"] == (
        "text/plain; version=0.0.4; charset=utf-8"
    )
    assert "# TYPE job_agent_http_requests_total counter" in prometheus.text
    assert 'endpoint="/api/auth/login"' in prometheus.text
    assert "secret" not in prometheus.text
    assert "request_id" not in prometheus.text
    assert "/internal/metrics" not in client.get("/openapi.json").json()["paths"]


def test_admin_account_status_change_is_visible_in_audit_log(tmp_path) -> None:
    """管理员账号状态变更应写入低敏审计日志，并保留 request_id。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "JOB_AGENT_BOOTSTRAP_ADMIN_EMAIL=audit-admin@example.com",
                "JOB_AGENT_BOOTSTRAP_ADMIN_PASSWORD=strong-password-123",
            ]
        ),
        encoding="utf-8",
    )
    app = create_web_app(env_file=env_path)
    admin_client = TestClient(app)
    user_client = TestClient(app)
    target = admin_client.post(
        "/api/auth/register",
        json={"email": "audit-target@example.com", "password": "password-123"},
    ).json()["account"]
    admin_login = admin_client.post(
        "/api/auth/login",
        json={"email": "audit-admin@example.com", "password": "strong-password-123"},
    )
    user_client.post(
        "/api/auth/register",
        json={"email": "audit-user@example.com", "password": "password-123"},
    )
    user_login = user_client.post(
        "/api/auth/login",
        json={"email": "audit-user@example.com", "password": "password-123"},
    )

    assert admin_login.status_code == 200
    assert user_login.status_code == 200
    blocked = user_client.get("/api/admin/audit/events")
    response = admin_client.patch(
        f"/api/admin/accounts/{target['id']}/status",
        headers={"X-Request-ID": "audit-request-123"},
        json={"status": "disabled"},
    )
    audit = admin_client.get("/api/admin/audit/events?limit=10")

    assert blocked.status_code == 403
    assert response.status_code == 200
    assert audit.status_code == 200
    event = audit.json()["events"][0]
    assert event["action"] == "account.status_updated"
    assert event["target_account_id"] == target["id"]
    assert event["target_type"] == "account"
    assert event["outcome"] == "succeeded"
    assert event["request_id"] == "audit-request-123"
    assert event["details"] == {
        "previous_status": "active",
        "next_status": "disabled",
        "target_role": "user",
    }
    assert "audit-target@example.com" not in event["summary"]


def test_web_bootstraps_initial_admin_once_from_env(tmp_path) -> None:
    """首次管理员只能由私有环境配置引导，公开注册接口不能提升普通用户。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "JOB_AGENT_BOOTSTRAP_ADMIN_EMAIL=admin@example.com",
                "JOB_AGENT_BOOTSTRAP_ADMIN_PASSWORD=strong-password-123",
                "JOB_AGENT_BOOTSTRAP_ADMIN_DISPLAY_NAME=初始管理员",
            ]
        ),
        encoding="utf-8",
    )
    client = TestClient(
        create_web_app(
            env_file=env_path,
        )
    )

    response = client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "strong-password-123"},
    )

    assert response.status_code == 200
    assert response.json()["account"]["role"] == "admin"

    # 再次创建 Web 应用不会重复创建或重置该账号的密码。
    restarted = TestClient(
        create_web_app(
            env_file=env_path,
        )
    )
    repeated_login = restarted.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "strong-password-123"},
    )

    assert repeated_login.status_code == 200


def test_web_home_page_and_assets_are_available(tmp_path):
    """本地 Web 应用可以打开首页，并加载前端静态资源。"""

    client = legacy_client()

    home = client.get("/")
    login_page = client.get("/login")
    workspace_page = client.get("/workspace")
    profile_page = client.get("/profile")
    admin_page = client.get("/admin")
    script = client.get("/static/app.js")
    styles = client.get("/static/styles.css")
    tokens = client.get("/static/tokens.css")
    vue = client.get("/static/vendor/vue.global.prod.js")

    assert home.status_code == 200
    for page in (login_page, workspace_page, profile_page, admin_page):
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store, max-age=0"
        assert "/static/app.js?v=20260825-project-collection-v3" in page.text
    assert "Job Hunting Agent" in home.text
    assert "syncAuthPageClass" in script.text
    assert "FRONTEND_ROUTES" in script.text
    assert "safeFrontendNextRoute" in script.text
    assert 'v-if="showAuthSurface"' in home.text
    assert 'v-if="showWorkspaceSurface"' in home.text
    assert 'v-if="showProfileSurface"' in home.text
    assert 'v-if="showAdminSurface"' in home.text
    assert 'v-if="showRouteLoading"' in home.text
    assert '/static/app.js?v=20260825-project-collection-v3' in home.text
    assert '/static/styles.css?v=20260825-project-collection-v3' in home.text
    assert 'class="account-menu-trigger"' in home.text
    assert 'id="workspaceAccountMenu"' in home.text
    assert 'role="menuitem" @click="openProfile"' in home.text
    assert 'role="menuitem" @click="logout"' in home.text
    assert 'class="account-badge"' not in home.text
    assert 'class="nav-link logout-link"' not in home.text
    assert "本地运行 · 用户复制职位文本" not in home.text
    assert "Conversation Workspace" not in home.text
    assert "整理求职证据" not in home.text
    assert "status-pill" not in home.text
    assert "mini-metrics" not in home.text
    assert "自动增量 RAG" not in home.text
    assert "使用 LangChain Agent（需 .env）" not in home.text
    assert "删除当前档案" in home.text
    assert "session-picker" in home.text
    assert "session-option-delete" in home.text
    assert "deleteCurrentSession" not in home.text
    assert "session-list" not in home.text
    assert "deleteJob(job)" in home.text
    assert "/api/me/balance" in script.text
    assert "/api/me/balance/recharge" in script.text
    assert "余额与消费记录" in home.text
    assert home.headers["cache-control"] == "no-store, max-age=0"
    assert script.status_code == 200
    assert script.headers["cache-control"] == "no-store, max-age=0"
    assert styles.status_code == 200
    assert styles.headers["cache-control"] == "no-store, max-age=0"
    assert "#app.auth-page" in styles.text
    assert ".route-loading" in styles.text
    assert "max-width: none" in styles.text
    assert tokens.status_code == 200
    assert "Hallmark · tokens" in tokens.text
    assert "oklch(" in tokens.text
    assert vue.status_code == 200
    assert "Vue" in vue.text


def test_web_profile_balance_and_simulated_recharge_are_account_scoped(tmp_path):
    """个人中心应展示初始余额，并把模拟充值写入同一账号的分页账本。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "JOB_AGENT_BILLING_PRICE_PER_MILLION_TOKENS_YUAN=25",
                "JOB_AGENT_BILLING_STARTING_BALANCE_YUAN=100",
                "JOB_AGENT_BILLING_LOW_BALANCE_THRESHOLD_YUAN=10",
            ]
        ),
        encoding="utf-8",
    )
    client = login_test_account(
        TestClient(create_web_app(env_file=env_path)),
        email="balance-profile@example.com",
    )

    initial = client.get("/api/me/balance")
    assert initial.status_code == 200, initial.text
    assert initial.json()["summary"]["balance_micro_yuan"] == 100_000_000
    assert initial.json()["summary"]["state"] == "balance"
    assert initial.json()["total"] == 1
    assert initial.json()["entries"][0]["entry_kind"] == "initial_credit"

    recharged = client.post(
        "/api/me/balance/recharge",
        json={
            "amount_yuan": 12.5,
            "note": "测试充值",
            "idempotency_key": "web-profile-recharge-1",
        },
    )
    assert recharged.status_code == 200, recharged.text
    assert recharged.json()["summary"]["balance_micro_yuan"] == 112_500_000
    assert recharged.json()["order"]["status"] == "paid"
    assert recharged.json()["entry"]["recharge_order_id"] == recharged.json()["order"]["id"]

    duplicate = client.post(
        "/api/me/balance/recharge",
        json={
            "amount_yuan": 12.5,
            "note": "测试充值",
            "idempotency_key": "web-profile-recharge-1",
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["order"]["id"] == recharged.json()["order"]["id"]
    assert duplicate.json()["summary"]["balance_micro_yuan"] == 112_500_000

    orders = client.get("/api/me/recharge/orders")
    assert orders.status_code == 200
    assert orders.json()["total"] == 1
    assert orders.json()["orders"][0]["payment_provider"] == "simulated"

    ledger = client.get("/api/me/balance?limit=1&offset=0")
    assert ledger.status_code == 200
    assert ledger.json()["total"] == 2
    assert ledger.json()["entries"][0]["entry_kind"] == "recharge"
    assert ledger.json()["entries"][0]["amount_micro_yuan"] == 12_500_000

    invalid = client.post("/api/me/balance/recharge", json={"amount_yuan": 0})
    assert invalid.status_code == 422


def test_admin_balance_summary_and_ledger_are_paginated(tmp_path):
    """管理员可以按账号读取余额流水，并和账号级余额投影保持一致。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "JOB_AGENT_BOOTSTRAP_ADMIN_EMAIL=balance-admin@example.com",
                "JOB_AGENT_BOOTSTRAP_ADMIN_PASSWORD=strong-password-123",
                "JOB_AGENT_BILLING_STARTING_BALANCE_YUAN=100",
            ]
        ),
        encoding="utf-8",
    )
    client = login_test_account(
        TestClient(create_web_app(env_file=env_path)),
        email="balance-admin@example.com",
        password="strong-password-123",
    )

    accounts = client.get("/api/admin/accounts")
    assert accounts.status_code == 200
    account_id = accounts.json()["accounts"][0]["id"]

    summary = client.get("/api/admin/usage/summary")
    assert summary.status_code == 200
    assert summary.json()["billing"]["summary"]["total_balance_micro_yuan"] == 100_000_000
    projection = summary.json()["billing"]["by_account"][0]
    assert projection["account_id"] == account_id
    assert projection["state"] == "balance"

    events = client.get(f"/api/admin/balance/events?account_id={account_id}&limit=1&offset=0")
    assert events.status_code == 200
    assert events.json()["total"] == 1
    assert events.json()["page_size"] == 100
    assert events.json()["max_pages"] is None
    assert events.json()["entries"][0]["entry_kind"] == "initial_credit"


def test_admin_can_credit_self_or_any_account_without_payment(tmp_path):
    """管理员补款不受生产模拟支付开关影响，并对任意目标账号保留审计。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "JOB_AGENT_BOOTSTRAP_ADMIN_EMAIL=manual-credit-admin@example.com",
                "JOB_AGENT_BOOTSTRAP_ADMIN_PASSWORD=strong-password-123",
                "JOB_AGENT_BILLING_STARTING_BALANCE_YUAN=0",
            ]
        ),
        encoding="utf-8",
    )
    client = TestClient(create_web_app(env_file=env_path))
    registered = client.post(
        "/api/auth/register",
        json={"email": "manual-credit-user@example.com", "password": "password-123"},
    )
    assert registered.status_code == 200
    client = login_test_account(
        client,
        email="manual-credit-admin@example.com",
        password="strong-password-123",
    )
    accounts = client.get("/api/admin/accounts").json()["accounts"]
    admin_id = next(item["id"] for item in accounts if item["role"] == "admin")
    user_id = next(item["id"] for item in accounts if item["role"] == "user")
    before_billing = client.get("/api/admin/usage/summary").json()["billing"]["by_account"]
    admin_before = next(item for item in before_billing if item["account_id"] == admin_id)
    user_before = next(item for item in before_billing if item["account_id"] == user_id)
    disabled = client.patch(f"/api/admin/accounts/{user_id}/status", json={"status": "disabled"})
    assert disabled.status_code == 200

    user_credit = client.post(
        f"/api/admin/accounts/{user_id}/balance/credit",
        json={
            "amount_yuan": 30,
            "reason": "支付异常人工补款",
            "idempotency_key": "web-admin-credit-user-1",
        },
    )
    self_credit = client.post(
        f"/api/admin/accounts/{admin_id}/balance/credit",
        json={
            "amount_yuan": 10,
            "reason": "管理员测试补款",
            "idempotency_key": "web-admin-credit-self-1",
        },
    )

    assert user_credit.status_code == 200, user_credit.text
    assert user_credit.json()["summary"]["balance_micro_yuan"] == user_before["balance_micro_yuan"] + 30_000_000
    assert (
        user_credit.json()["summary"]["total_recharge_micro_yuan"]
        == user_before["total_recharge_micro_yuan"]
    )
    assert user_credit.json()["entry"]["operator_account_id"] == admin_id
    assert user_credit.json()["entry"]["entry_kind"] == "adjustment"
    assert self_credit.status_code == 200, self_credit.text
    assert self_credit.json()["summary"]["balance_micro_yuan"] == admin_before["balance_micro_yuan"] + 10_000_000

    duplicate = client.post(
        f"/api/admin/accounts/{user_id}/balance/credit",
        json={
            "amount_yuan": 30,
            "reason": "支付异常人工补款",
            "idempotency_key": "web-admin-credit-user-1",
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["entry"]["id"] == user_credit.json()["entry"]["id"]
    assert duplicate.json()["summary"]["balance_micro_yuan"] == user_before["balance_micro_yuan"] + 30_000_000

    audit = client.get("/api/admin/audit/events?limit=20")
    assert audit.status_code == 200
    manual_events = [event for event in audit.json()["events"] if event["action"] == "balance.manual_credit"]
    assert len(manual_events) == 2
    assert {event["target_account_id"] for event in manual_events} == {admin_id, user_id}


def test_regular_account_cannot_use_admin_manual_credit(tmp_path):
    """普通账号不能调用管理员人工补款接口。"""

    client = login_test_account(
        TestClient(create_web_app(env_file=tmp_path / ".env")),
        email="manual-credit-denied@example.com",
    )
    account_id = client.get("/api/auth/me").json()["account"]["id"]
    response = client.post(
        f"/api/admin/accounts/{account_id}/balance/credit",
        json={
            "amount_yuan": 10,
            "reason": "越权补款",
            "idempotency_key": "web-admin-credit-denied-1",
        },
    )
    assert response.status_code == 403


def test_profile_delete_button_is_idle_without_a_selected_profile(tmp_path):
    """空档案状态不能把两个默认值 0 误判为正在删除。"""

    client = legacy_client()
    home = client.get("/").text
    script = client.get("/static/app.js").text
    button = re.search(
        r'<button\b[^>]*@click="deleteCurrentProfile"[^>]*>[\s\S]*?</button>',
        home,
    )

    assert button is not None
    assert "isDeletingCurrentProfile" in button.group(0)
    assert "deletingProfileId === currentProfileId" not in button.group(0)
    assert "isDeletingCurrentProfile()" in script
    assert "return Boolean(this.currentProfileId) && this.deletingProfileId === this.currentProfileId;" in script


def test_web_health_reports_enabled_memory_as_configured(tmp_path):
    """记忆配置成功且启用时，健康接口不能错误显示为未配置。"""

    client = legacy_client()

    health = client.get("/api/health")

    assert health.status_code == 200
    assert health.json()["memory"]["configured"] is True
    assert health.json()["memory"]["checkpoint_backend"] == "database"
    assert health.json()["model_circuit"]["state"] in {"not_started", "closed"}
    assert "billing" in health.json()
    assert isinstance(health.json()["billing"]["configured"], bool)
    assert health.json()["project_visual_analysis"]["enabled"] is True


def test_web_auth_bootstrap_does_not_surface_probe_errors_in_login_form(tmp_path):
    """初始化 Session 探测失败时，不应把错误提前显示成登录失败。"""

    client = legacy_client()
    script = client.get("/static/app.js").text
    check_auth_start = script.index("async checkAuth()")
    check_auth_end = script.index("/** 切换登录与注册表单。 */", check_auth_start)
    check_auth_body = script[check_auth_start:check_auth_end]

    assert "this.authError =" not in check_auth_body


def test_web_frontend_defaults_to_agent_and_incremental_rag_without_toggles(tmp_path):
    """网页聊天不再暴露模式开关，而是固定走 Agent + 自动增量 RAG。"""

    client = legacy_client()

    home = client.get("/").text
    script = client.get("/static/app.js").text

    assert 'v-model="autoRag"' not in home
    assert 'v-model="useLlm"' not in home
    assert "DEFAULT_USE_LANGCHAIN_AGENT = true" in script
    assert "DEFAULT_AUTO_INCREMENTAL_RAG = true" in script
    assert "use_env_llm: DEFAULT_USE_LANGCHAIN_AGENT" in script
    assert "auto_rag: DEFAULT_AUTO_INCREMENTAL_RAG" in script
    assert "this.useLlm =" not in script


def test_web_profile_form_uses_city_picker_and_auth_copy(tmp_path):
    """保留认证与学历约束，并用热门城市加省市二级菜单选择首选城市。"""

    client = legacy_client()

    home = client.get("/").text
    script = client.get("/static/app.js").text
    cities = client.get("/static/china_cities.js")
    styles = client.get("/static/styles.css").text

    assert ':type="authMode === \'register\' || authPasswordVisible ? \'text\' : \'password\'"' in home
    assert 'class="password-toggle"' in home
    assert ':aria-label="authPasswordVisible ? \'隐藏密码\' : \'显示密码\'"' in home
    assert "authPasswordVisible: false" in script
    assert "toggleAuthPassword()" in script
    assert "::-ms-reveal" in styles
    assert 'placeholder="例如：小林"' not in home
    assert 'placeholder="Python=项目使用,FastAPI=待确认"' not in home
    assert 'placeholder="AI Agent 应用开发"' not in home
    assert "Local Boundary" not in home
    assert "运行边界" not in home
    assert home.count("退出所有设备") == 2
    assert ':title="auth.account?.email || auth.account?.display_name || \'账号\'"' in home
    assert '{{ auth.account?.email || auth.account?.display_name || "账号" }}' in home
    assert home.count('auth.account?.email || auth.account?.display_name || "账号"') == 3
    for education in ("高中及以下", "大专", "本科", "硕士", "博士"):
        assert f'<option value="{education}">{education}</option>' in home
    assert 'class="city-picker"' in home
    assert 'aria-haspopup="dialog"' in home
    assert 'v-if="cityPickerOpen"' in home
    assert 'v-for="province in cityGroups"' in home
    assert 'v-for="city in activeCityOptions"' in home
    assert "热门城市" in home
    assert "省份及直辖市" in home
    assert '<optgroup' not in home
    assert '/static/china_cities.js?v=20260803-cities' in home
    assert '/static/styles.css?v=20260825-project-collection-v3' in home
    assert '/static/app.js?v=20260825-project-collection-v3' in home
    assert "cityGroups: buildSortedCityGroups()" in script
    assert "HOT_CITY_NAMES" in script
    assert "cityPickerOpen: false" in script
    assert 'activeCityProvince: "hot"' in script
    assert 'v-for="city in profileForm.preferredCities"' in home
    assert "preferred_cities: [...this.profileForm.preferredCities]" in script
    assert 'addPreferredCity(cityValue = "")' in script
    assert "togglePreferredCity(cityValue)" in script
    assert "isPreferredCity(cityValue)" in script
    assert "removePreferredCity(city)" in script
    assert ".city-picker-menu" in styles
    assert cities.status_code == 200
    assert "北京市" in cities.text
    assert "广州市" in cities.text
    assert "乌鲁木齐市" in cities.text


def test_web_can_create_profile_and_ingest_chat_message_incrementally(tmp_path):
    """网页 API 可以创建候选人档案，并通过聊天消息自动入库和增量索引。"""

    client = legacy_client()
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
    assert chat.json()["task_trace"]["status"] == "completed"
    assert {"used_tools", "tool_outputs", "usage", "result"}.isdisjoint(chat.json())
    assert profile["education"] == "本科"
    assert profile["skills"]["Python"] == "待确认"
    assert any("FastAPI" in item["content"] for item in rag["results"])


def test_web_rejects_exact_duplicate_profile_and_job_per_account(tmp_path):
    """同账号重复保存档案或职位必须返回 409，其他账号仍可独立保存。"""

    profile_payload = {
        "name": "小林",
        "status": "离职",
        "education": "本科",
        "experience_years": 1,
        "skills": {"Python": "项目使用", "FastAPI": "项目使用"},
        "preferred_cities": ["杭州市", "上海市"],
        "acceptable_cities": [],
        "salary_floor_k": 10,
        "expected_salary_k": 15,
        "target_directions": ["Python 后端开发"],
        "unacceptable": [],
    }
    job_payload = {
        "raw_text": """
        Python 后端开发工程师
        15-20K
        杭州
        1-3年
        本科
        职位描述：负责 Python 和 FastAPI 后端开发。
        """,
        "source_url": "https://www.zhipin.com/job_detail/example.html",
    }
    owner = legacy_client()
    other = login_test_account(TestClient(create_web_app()), "dedup-other@example.com")

    created = owner.post("/api/profiles", json=profile_payload)
    duplicate_profile = owner.post("/api/profiles", json={**profile_payload, "preferred_cities": ["上海市", "杭州市"]})
    other_profile = other.post("/api/profiles", json=profile_payload)
    imported = owner.post("/api/jobs", json=job_payload)
    duplicate_job = owner.post("/api/jobs", json={**job_payload, "raw_text": job_payload["raw_text"].replace("\n", "\r\n")})
    other_job = other.post("/api/jobs", json=job_payload)

    assert created.status_code == 200
    assert duplicate_profile.status_code == 409
    assert "候选人档案" in duplicate_profile.json()["detail"]
    assert other_profile.status_code == 200
    assert imported.status_code == 200
    assert duplicate_job.status_code == 409
    assert "职位信息" in duplicate_job.json()["detail"]
    assert other_job.status_code == 200
    assert len(owner.get("/api/profiles").json()["profiles"]) == 1
    assert len(owner.get("/api/jobs").json()["jobs"]) == 1


def test_profile_content_fingerprint_tracks_conversation_updates(tmp_path):
    """对话更新后的档案指纹也必须更新，才能继续拦住等价的新建档案。"""

    app = JobHuntingApp(semantic_matching=False)
    app.initialize()
    account = app.auth.register("fingerprint-update@example.com", "password-123")
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
        account_id=account.id,
    )
    app.store.update_candidate_profile(
        candidate_id,
        CandidateProfilePatch(skills={"Python": "待确认"}),
        account_id=account.id,
    )

    duplicate = CandidateProfileInput(
        name="小林",
        status="待补充",
        education="本科",
        experience_years=0,
        skills={"Python": "待确认"},
        preferred_cities=[],
        salary_floor_k=None,
        expected_salary_k=None,
        target_directions=[],
        unacceptable=[],
    )
    response = login_test_account(TestClient(create_web_app()), "fingerprint-update@example.com").post(
        "/api/profiles",
        json={
            "name": duplicate.name,
            "status": duplicate.status,
            "education": duplicate.education,
            "experience_years": duplicate.experience_years,
            "skills": duplicate.skills,
            "preferred_cities": duplicate.preferred_cities,
            "acceptable_cities": duplicate.acceptable_cities,
            "salary_floor_k": duplicate.salary_floor_k,
            "expected_salary_k": duplicate.expected_salary_k,
            "target_directions": duplicate.target_directions,
            "unacceptable": duplicate.unacceptable,
        },
    )

    assert response.status_code == 409


def test_web_frontend_shows_centered_duplicate_dialog(tmp_path):
    """所有 409 重复冲突都应走同一个居中弹窗，而非散落在各表单错误区。"""

    client = legacy_client()
    home = client.get("/").text
    script = client.get("/static/app.js").text
    styles = client.get("/static/styles.css").text

    assert 'class="duplicate-dialog"' in home
    assert 'role="alertdialog"' in home
    assert "showDuplicateNotice(error" in script
    assert "duplicate-dialog-lock" in script
    assert "error.status = response.status" in script
    assert ".duplicate-dialog" in styles
    assert ".duplicate-dialog-panel" in styles


def test_web_can_import_job_and_return_matches(tmp_path):
    """网页 API 可以导入候选人复制回来的 BOSS 职位文本，并返回匹配结果。"""

    client = legacy_client()
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


def test_web_can_delete_profiles_sessions_and_jobs_with_account_scoping(tmp_path):
    """网页删除接口应清理从属数据，并拒绝其他账号跨边界删除。"""

    client_a = TestClient(create_web_app())
    client_b = TestClient(create_web_app())

    for client, email in ((client_a, "delete-a@example.com"), (client_b, "delete-b@example.com")):
        assert client.post(
            "/api/auth/register",
            json={"email": email, "password": "password-123"},
        ).status_code == 200
        assert client.post(
            "/api/auth/login",
            json={"email": email, "password": "password-123"},
        ).status_code == 200

    candidate_id = client_a.post(
        "/api/profiles",
        json={
            "name": "待删除档案",
            "status": "待补充",
            "education": "本科",
            "experience_years": 1,
            "skills": {"Python": "熟悉"},
            "preferred_cities": ["杭州"],
            "target_directions": ["后端开发"],
            "unacceptable": [],
        },
    ).json()["candidate_id"]
    job = client_a.post(
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
        },
    ).json()["job"]
    session_id = client_a.post(
        "/api/chat/sessions",
        json={"candidate_id": candidate_id, "title": "待删除对话", "job_id": job["id"]},
    ).json()["session"]["session_id"]
    chat = client_a.post(
        "/api/chat",
        json={
            "candidate_id": candidate_id,
            "session_id": session_id,
            "message": "我是本科，有 Python 项目经验。",
            "use_env_llm": False,
            "auto_rag": False,
        },
    )
    assert chat.status_code == 200, chat.text
    assert client_a.get("/api/chat/history", params={"candidate_id": candidate_id, "session_id": session_id}).json()["messages"]

    backend = client_a.app.state.backend
    account = backend.store.get_account_by_email("delete-a@example.com")[0]
    project_card = backend.store.save_project_card(
        candidate_id,
        ProjectExperienceCard(
            card_type="待确认项目经历卡片",
            project_name="待删除项目",
            read_files=["README.md"],
            skipped_summary={},
            detected_tech_stack=["Python", "FastAPI"],
            detected_core_features=["接口/API 服务"],
            responsibility_draft=["负责接口设计"],
            highlight_draft=["完成后端服务拆分"],
            resume_expression_draft=["使用 Python 和 FastAPI 开发接口"],
            questions_for_candidate=[],
            source_type="github_public_repository",
            source_url="https://github.com/example/delete-project",
            source_ref="main",
        ),
        account_id=account.id,
    )
    confirmed_project, _ = backend.confirm_project_card_and_enqueue_rag(
        project_card.id,
        "本人负责接口设计与实现。",
        account_id=account.id,
    )
    project_long_texts = backend.store.list_long_texts(
        ["project_experience_card"],
        candidate_id=candidate_id,
        account_id=account.id,
    )
    assert confirmed_project.status == "已确认"
    assert len(project_long_texts) == 1
    backend.index_rag_long_texts([project_long_texts[0].id], account_id=account.id)
    assert client_a.get("/api/projects", params={"candidate_id": candidate_id}).json()["project_cards"]
    assert backend.search_rag("接口设计", account_id=account.id)

    assert client_b.delete(f"/api/profiles/{candidate_id}").status_code == 404
    assert client_b.delete(f"/api/jobs/{job['id']}").status_code == 404
    assert client_b.delete(f"/api/projects/{project_card.id}").status_code == 404

    deleted_project = client_a.delete(f"/api/projects/{project_card.id}")
    assert deleted_project.status_code == 200, deleted_project.text
    deleted_project_payload = deleted_project.json()
    assert deleted_project_payload["deleted"] is True
    assert deleted_project_payload["rag_cleanup"] == "database_cascade"
    assert deleted_project_payload["long_text_ids"] == [project_long_texts[0].id]
    assert client_a.get("/api/projects", params={"candidate_id": candidate_id}).json()["project_cards"] == []
    assert backend.store.list_long_texts(
        ["project_experience_card"],
        candidate_id=candidate_id,
        account_id=account.id,
    ) == []
    assert backend.search_rag("接口设计", account_id=account.id) == []

    deleted_session = client_a.delete(f"/api/chat/sessions/{session_id}")
    assert deleted_session.status_code == 200, deleted_session.text
    assert client_a.get(f"/api/chat/sessions?candidate_id={candidate_id}").json()["sessions"] == []

    deleted_profile = client_a.delete(f"/api/profiles/{candidate_id}")
    assert deleted_profile.status_code == 200, deleted_profile.text
    assert client_a.get("/api/profiles").json()["profiles"] == []
    assert client_a.get("/api/jobs").json()["jobs"] == [job]

    deleted_job = client_a.delete(f"/api/jobs/{job['id']}")
    assert deleted_job.status_code == 200, deleted_job.text
    assert client_a.get("/api/jobs").json()["jobs"] == []


def test_web_rejects_non_job_text_before_saving(tmp_path):
    """导入职位前应审核文本；非招聘职位内容不能进入职位池。"""

    client = legacy_client()

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

    client = legacy_client()

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

    backend = JobHuntingApp()
    backend.initialize()
    account = backend.auth.register("legacy-job@example.com", "password-123")
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
        ),
        account_id=account.id,
    )

    with backend.store.connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                account_id, raw_text, source_url, title, city, salary_min_k, salary_max_k,
                salary_months, salary_unit, experience_min_years,
                experience_max_years, experience_label, education,
                company_name, industry, company_size, skills_json,
                description_text, field_confidence_json, uncertainty_notes_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account.id,
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

    client = login_test_account(
        TestClient(create_web_app()),
        email="legacy-job@example.com",
    )

    assert client.get("/api/jobs").json()["jobs"] == []
    assert client.get(f"/api/matches/{candidate_id}").json()["matches"] == []


def test_web_frontend_loads_persisted_jobs_on_page_open(tmp_path):
    """页面脚本应在打开时主动拉取已导入职位，而不是只在匹配接口里临时使用。"""

    client = legacy_client()
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
    assert "async loadJobs(signal = null)" in script
    assert "await this.loadJobs();" in script
    assert "jobImportError" in script
    assert "matchDetailGroups(match)" in script
    assert "匹配分数构成" in home


def test_web_can_save_manual_job_skill_categories(tmp_path):
    """网页可以调整已有职位技能分类，并通过接口返回更新后的职位。"""

    client = legacy_client()
    job = client.post(
        "/api/jobs",
        json={
            "raw_text": """
            Python 后端开发工程师
            15-20K
            杭州
            1-3年
            本科
            职位描述：负责 Python 和 Docker 后端服务开发。
            """,
        },
    ).json()["job"]

    requirements = [
        {**item, "category": "core" if item["name"] == "Python" else "bonus"}
        for item in job["skill_requirements"]
    ]
    response = client.put(
        f"/api/jobs/{job['id']}/skill-requirements",
        json={"requirements": requirements},
    )

    assert response.status_code == 200
    updated = response.json()["job"]["skill_requirements"]
    assert any(item["name"] == "Python" and item["category"] == "core" for item in updated)
    assert any(item["name"] == "Docker" and item["category"] == "bonus" for item in updated)


def test_web_chat_history_survives_page_reopen(tmp_path):
    """网页聊天记录应保存到 PostgreSQL，刷新或重新打开页面后可以恢复。"""

    client = legacy_client()
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
    reopened_client = legacy_client()
    history = reopened_client.get(
        "/api/chat/history",
        params={"candidate_id": candidate_id, "session_id": f"web-candidate-{candidate_id}"},
    )

    assert chat.status_code == 200
    assert history.status_code == 200
    messages = history.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "我是本科，1年经验，会 Python。"
    assert "保存字段" not in messages[1]["content"]
    assert "工具：" not in messages[1]["content"]


def test_web_chat_reply_does_not_expose_internal_execution_metadata(tmp_path):
    """新消息的聊天气泡不能包含工具名、保存字段、ID 或 RAG 状态。"""

    rendered = format_web_chat_reply(
        mode="rule_based_ingestion",
        reply="已为你更新档案。",
        used_tools=["ingest_candidate_message"],
        tool_outputs=[
            {
                "tool_name": "ingest_candidate_message",
                "data": {
                    "saved_structured_fields": ["skills"],
                    "saved_long_text_ids": [28],
                    "rag_update_mode": "incremental",
                },
            }
        ],
        rule_based_result={
            "saved_structured_fields": ["skills"],
            "saved_long_text_ids": [28],
            "rag_update_mode": "incremental",
        },
    )

    assert rendered == "已为你更新档案。"
    assert "ingest_candidate_message" not in rendered
    assert "保存字段" not in rendered
    assert "长文本 ID" not in rendered
    assert "RAG" not in rendered


def test_web_chat_history_hides_legacy_internal_execution_metadata(tmp_path):
    """刷新历史消息时也要隐藏旧版本已经持久化的内部字段。"""

    client = login_test_account(
        TestClient(create_web_app()),
        email="legacy-chat-metadata@example.com",
    )
    candidate_id = client.post(
        "/api/profiles",
        json={
            "name": "历史消息清理测试",
            "status": "待补充",
            "education": "本科",
            "experience_years": 1,
            "skills": {},
            "preferred_cities": [],
            "expected_salary_k": None,
            "salary_floor_k": None,
            "target_directions": [],
            "unacceptable": [],
        },
    ).json()["candidate_id"]
    account_id = client.get("/api/auth/me").json()["account"]["id"]
    backend = JobHuntingApp()
    backend.initialize()
    session_id = f"legacy-metadata-{candidate_id}"
    backend.store.create_chat_session(
        session_id=session_id,
        account_id=account_id,
        candidate_id=candidate_id,
        title="历史消息清理测试",
    )
    backend.save_chat_message(
        candidate_id,
        session_id,
        "assistant",
        "已为你更新档案。\n\n工具：ingest_candidate_message\n\n保存字段：skills\n长文本 ID：28\nRAG：已增量索引本次长文本",
        {"source": "legacy-test"},
        account_id=account_id,
    )

    history = client.get(
        "/api/chat/history",
        params={"candidate_id": candidate_id, "session_id": session_id},
    )

    assert history.status_code == 200
    assert history.json()["messages"][0]["content"] == "已为你更新档案。"
    assert history.json()["messages"][0]["metadata"] == {}


def test_chat_metadata_sanitizer_only_removes_exact_legacy_tail_blocks() -> None:
    """正常回答中的同名小标题不能因为旧版清理规则被截断。"""

    normal_reply = "分析如下：\n匹配结果：该岗位与候选人的经历整体相关，但仍需核对项目证据。"
    legacy_only = "工具：ingest_candidate_message\n保存字段：skills\n长文本 ID：28"

    assert sanitize_web_chat_reply(normal_reply) == normal_reply
    assert sanitize_web_chat_reply(legacy_only) == ""


def test_web_chat_stream_returns_updated_profile_skill_proficiency(tmp_path):
    """网页对话中的明确技能熟练度必须同步到 SSE final 的档案摘要。"""

    client = legacy_client()
    candidate_id = client.post(
        "/api/profiles",
        json={
            "name": "技能熟练度网页测试",
            "status": "待补充",
            "education": "本科",
            "experience_years": 1,
            "skills": {"Python": "待确认"},
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
            "message": "我的python熟练度是精通",
            "auto_rag": False,
            "use_env_llm": False,
            "session_id": f"web-skill-proficiency-{candidate_id}",
        },
    )

    assert response.status_code == 200
    final_match = re.search(r"event: final\r?\ndata: (.+)", response.text)
    assert final_match is not None
    final_payload = json.loads(final_match.group(1))
    assert final_payload["profile"]["skills"]["Python"] == "精通"
    assert (
        client.get(f"/api/profiles/{candidate_id}").json()["profile"]["skills"]["Python"]
        == "精通"
    )


def test_web_chat_can_use_langchain_agent_mode(tmp_path):
    """网页聊天在开启开关时，会走标准 LangChain Agent 主流程。"""

    agent_backend = JobHuntingApp()
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
        chat_agent=JobHuntingAgent(
            app=agent_backend,
            model=model,
        ),
    )
    client = login_test_account(TestClient(backend_app))
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
    assert chat.json()["display_reply"] == "我已经通过 Agent 工具保存了你的资料。"
    assert {"used_tools", "tool_outputs", "usage", "result"}.isdisjoint(chat.json())


def test_web_chat_stream_returns_sse_and_saves_history(tmp_path):
    """网页流式聊天接口会返回 SSE 事件，并在 final 后保存聊天历史。"""

    agent_backend = JobHuntingApp()
    agent_backend.initialize()
    model = ToolCallingFakeChatModel(responses=[AIMessage(content="这是流式回复。")])
    backend_app = create_web_app(
        chat_agent=JobHuntingAgent(
            app=agent_backend,
            model=model,
        ),
    )
    client = login_test_account(TestClient(backend_app))
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
    final_match = re.search(r"event: final\r?\ndata: (.+)", response.text)
    assert final_match is not None
    final_payload = json.loads(final_match.group(1))
    assert {"used_tools", "tool_outputs", "usage", "result", "root_request_id"}.isdisjoint(
        final_payload
    )
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[1]["content"].startswith("这是流式回复。")


def test_web_chat_stream_preserves_multiple_token_events(tmp_path):
    """底层模型支持 token stream 时，Web SSE 也必须向前端转发多个 token。"""

    agent_backend = JobHuntingApp()
    agent_backend.initialize()
    model = StreamingFakeChatModel(responses=["流式OK"])
    backend_app = create_web_app(
        chat_agent=JobHuntingAgent(
            app=agent_backend,
            model=model,
        ),
    )
    client = login_test_account(TestClient(backend_app))
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


def test_web_chat_stream_without_tools_does_not_create_admin_tool_trace(tmp_path):
    """没有真实工具调用的流式消息，不应写入管理端工具审计。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "JOB_AGENT_BOOTSTRAP_ADMIN_EMAIL=admin@example.com",
                "JOB_AGENT_BOOTSTRAP_ADMIN_PASSWORD=strong-password-123",
                "JOB_AGENT_BOOTSTRAP_ADMIN_DISPLAY_NAME=初始管理员",
            ]
        ),
        encoding="utf-8",
    )
    agent_backend = JobHuntingApp()
    agent_backend.initialize()
    model = ToolCallingFakeChatModel(responses=[AIMessage(content="你好，我只是打个招呼。")])
    backend_app = create_web_app(
        env_file=env_path,
        chat_agent=JobHuntingAgent(
            app=agent_backend,
            model=model,
        ),
    )
    client = login_test_account(
        TestClient(backend_app),
        email="admin@example.com",
        password="strong-password-123",
    )
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
            "message": "请先问好，不要调用工具。",
            "auto_rag": False,
            "use_env_llm": True,
            "session_id": f"web-candidate-{candidate_id}",
        },
    )

    assert response.status_code == 200
    assert "event: final" in response.text
    assert "event: step_started" not in response.text
    assert client.get("/api/admin/usage/summary").json()["tool_calls_by_account"] == []
    assert client.get("/api/admin/tools/traces").json()["traces"] == []


def test_admin_usage_events_keep_five_page_window_and_prune_old_rows(tmp_path):
    """后台 Token 明细只保留最近 5 页，并会删除更早流水。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "JOB_AGENT_BOOTSTRAP_ADMIN_EMAIL=admin@example.com",
                "JOB_AGENT_BOOTSTRAP_ADMIN_PASSWORD=strong-password-123",
                "JOB_AGENT_BOOTSTRAP_ADMIN_DISPLAY_NAME=初始管理员",
            ]
        ),
        encoding="utf-8",
    )
    web_app = create_web_app(env_file=env_path)
    client = login_test_account(
        TestClient(web_app),
        email="admin@example.com",
        password="strong-password-123",
    )
    store = web_app.state.backend.store
    account = store.get_account_by_email("admin@example.com")[0]

    def ledger_timestamp(index: int) -> str:
        minute, second = divmod(index, 60)
        return f"2026-08-21T00:{minute:02d}:{second:02d}+00:00"

    def usage_event(call_id: str, total_tokens: int, created_at: str) -> UsageEventRecord:
        return UsageEventRecord(
            id=0,
            account_id=account.id,
            candidate_id=None,
            session_id=None,
            root_request_id=f"request-{call_id}",
            call_id=call_id,
            provider="test-provider",
            model="test-model",
            operation="agent_chat",
            input_tokens=total_tokens,
            output_tokens=0,
            total_tokens=total_tokens,
            usage_source="provider",
            status="succeeded",
            attempt=1,
            provider_request_id=None,
            raw_usage={"total_tokens": total_tokens},
            created_at=created_at,
            billable=True,
            pricing_version="test-v1",
        )

    for index in range(1, 502):
        store.record_usage_event(
            usage_event(f"usage-{index:03d}", 1, ledger_timestamp(index))
        )

    response = client.get(f"/api/admin/usage/events?account_id={account.id}&limit=100&offset=0")
    assert response.status_code == 200
    assert response.json()["total"] == 500
    assert response.json()["page_size"] == 100
    assert response.json()["max_pages"] == 5
    assert [event["call_id"] for event in response.json()["events"]] == [
        f"usage-{index:03d}" for index in range(501, 401, -1)
    ]

    response_page5 = client.get(f"/api/admin/usage/events?account_id={account.id}&limit=100&offset=400")
    assert response_page5.status_code == 200
    assert [event["call_id"] for event in response_page5.json()["events"]] == [
        f"usage-{index:03d}" for index in range(101, 1, -1)
    ]

    assert store.count_usage_events(account.id) == 500
    assert [event.call_id for event in store.list_usage_events(account_id=account.id, limit=100, offset=0)] == [
        f"usage-{index:03d}" for index in range(501, 401, -1)
    ]
    summary = client.get("/api/admin/usage/summary").json()
    assert summary["summary"]["total_tokens"] == 500
    assert summary["summary"]["event_count"] == 500
    assert summary["by_account"] == [
        {
            "account_id": account.id,
            "input_tokens": 500,
            "output_tokens": 0,
            "total_tokens": 500,
            "billable_tokens": 500,
            "event_count": 500,
        }
    ]
    assert summary["tool_calls_by_account"] == []
    assert summary["billing"]["summary"]["configured"] is True
    assert summary["billing"]["summary"]["account_count"] == 1
    assert summary["billing"]["by_account"][0]["state"] == "balance"

def test_web_chat_stream_records_admin_tool_trace_detail(tmp_path):
    """真实工具调用的流式消息应能在管理端看到同一条任务链路与步骤结果。"""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "JOB_AGENT_BOOTSTRAP_ADMIN_EMAIL=admin@example.com",
                "JOB_AGENT_BOOTSTRAP_ADMIN_PASSWORD=strong-password-123",
                "JOB_AGENT_BOOTSTRAP_ADMIN_DISPLAY_NAME=初始管理员",
            ]
        ),
        encoding="utf-8",
    )
    agent_backend = JobHuntingApp()
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
            AIMessage(content="我已经通过工具保存了你的资料。"),
        ]
    )
    backend_app = create_web_app(
        env_file=env_path,
        chat_agent=JobHuntingAgent(
            app=agent_backend,
            model=model,
        ),
    )
    client = login_test_account(
        TestClient(backend_app),
        email="admin@example.com",
        password="strong-password-123",
    )
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
            "message": "我是本科，1年经验，会 Python 和 FastAPI。",
            "auto_rag": True,
            "use_env_llm": True,
            "session_id": f"web-candidate-{candidate_id}",
        },
    )

    assert response.status_code == 200, response.text
    assert "event: step_started" in response.text
    assert "event: step_completed" in response.text
    assert "event: final" in response.text

    summary = client.get("/api/admin/usage/summary").json()
    tool_counts = summary["tool_calls_by_account"]
    assert len(tool_counts) == 1
    assert tool_counts[0]["trace_count"] == 1
    assert tool_counts[0]["failed_trace_count"] == 0

    trace_list = client.get("/api/admin/tools/traces").json()
    assert trace_list["total"] == 1
    assert len(trace_list["traces"]) == 1
    assert trace_list["traces"][0]["step_count"] == 1
    trace_id = trace_list["traces"][0]["root_request_id"]

    detail = client.get(f"/api/admin/tools/traces/{trace_id}").json()["trace"]
    assert detail["root_request_id"] == trace_id
    assert detail["trace"]["steps"][0]["name"] == "ingest_candidate_message"
    assert detail["trace"]["steps"][0]["summary"].startswith("已保存")


def test_web_chat_bubble_renders_final_markdown_without_stream_reparse(tmp_path):
    """流式阶段展示纯文本，定稿后仍保留安全 Markdown 渲染。"""

    client = legacy_client()
    script = client.get("/static/app.js").text
    home = client.get("/").text

    # 流式阶段不应在每个 token 上重跑完整 Markdown 解析；定稿后仍要通过
    # 安全 HTML 缓存保留 **加粗**、列表和代码块展示。
    assert 'id="app"' in home
    assert "/static/vendor/vue.global.prod.js?v=20260731-auth-admin" in home
    assert 'v-if="message.isStreaming"' in home
    assert 'v-html="message.renderedHtml"' in home
    assert 'v-html="renderMarkdown(message.content)"' not in home
    assert "Vue.createApp" in script or "createApp({" in script
    assert "renderMarkdown(text)" in script
    assert "renderedHtml" in script
    assert "isStreaming" in script
    assert '"/api/chat/stream"' in script
    assert "response.body.getReader()" in script
    assert "splitStreamDisplayChunks(content)" in script
    assert "requestAnimationFrame" in script
    assert "v-cloak" in home


def test_web_chat_stream_has_timeout_and_cancel_path(tmp_path):
    """模型或网络长时间无响应时，前端必须能超时或主动停止生成。"""

    client = legacy_client()
    script = client.get("/static/app.js").text
    home = client.get("/").text

    assert "CHAT_STREAM_TIMEOUT_MS" in script
    assert "chatAbortController" in script
    assert "cancelChat()" in script
    assert "controller.abort()" in script
    assert "reader.cancel()" in script
    assert "ChatStreamTimeoutError" in script
    assert "停止生成" in home


def test_web_stream_message_keeps_vue_reactive_proxy(tmp_path):
    """流式更新必须持有 Vue 数组里的 Proxy，不能继续修改 push 前的原始对象。"""

    client = legacy_client()

    script = client.get("/static/app.js").text

    # Vue 3 不会追踪通过原始对象引用执行的属性修改。push 后重新从响应式数组读取，
    # 才能保证每个 token 都触发气泡重绘，而不是等其他状态变化后一次性显示全文。
    assert "const reactiveIndex = this.messages.push(message) - 1;" in script
    assert "return this.messages[reactiveIndex];" in script


def test_web_markdown_renderer_supports_tables(tmp_path):
    """模型输出标准 Markdown 表格时，前端必须生成表格 DOM 和响应式滚动容器。"""

    client = legacy_client()

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
