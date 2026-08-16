"""职位截图转写边界测试。"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from PIL import Image

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.job_screenshot import (
    JobScreenshot,
    JobScreenshotError,
    JobScreenshotExtractor,
)
from job_hunting_agent.web import create_web_app


def png_bytes() -> bytes:
    """创建最小有效 PNG，避免把二进制夹具写入仓库。"""

    output = BytesIO()
    Image.new("RGB", (24, 24), color="white").save(output, format="PNG")
    return output.getvalue()


class RecordingChatModel:
    """记录多模态消息并返回固定职位文本的轻量测试替身。"""

    def __init__(self, response: str):
        self.response = response
        self.requests: list[object] = []

    def invoke(self, message: object) -> AIMessage:
        self.requests.append(message)
        return AIMessage(content=self.response)


class RecordingGateway:
    """只实现截图转写所需的 Gateway 小接口。"""

    def __init__(self, response: str):
        self.model = RecordingChatModel(response)
        self.contexts: list[SimpleNamespace] = []
        self.recorded: list[tuple[object, object]] = []

    def new_call_context(self, operation: str, *, account_id: int | None = None) -> SimpleNamespace:
        context = SimpleNamespace(operation=operation, account_id=account_id)
        self.contexts.append(context)
        return context

    def chat_model(self, operation: str, temperature: float = 0) -> RecordingChatModel:
        assert operation == "job_screenshot_extraction"
        assert temperature == 0
        return self.model

    def record_chat_response(self, context: object, response: object) -> None:
        self.recorded.append((context, response))


def test_job_screenshot_extractor_uses_model_gateway_and_returns_visible_job_text() -> None:
    """模型只能看到用户上传的图片，并返回可交给职位解析器的纯文本。"""

    gateway = RecordingGateway(
        """{
"is_job": true,
"confidence": 0.97,
"job_text": "Python 后端开发工程师\\n15-20K\\n杭州\\n1-3年\\n本科\\n职位描述：负责 Python 和 FastAPI 后端开发。"
}"""
    )
    extractor = JobScreenshotExtractor(gateway)

    extracted = extractor.extract(
        [JobScreenshot(content=png_bytes(), media_type="image/png")],
        account_id=7,
    )

    assert extracted.startswith("Python 后端开发工程师")
    assert gateway.contexts[0].operation == "job_screenshot_extraction"
    assert gateway.contexts[0].account_id == 7
    assert len(gateway.recorded) == 1
    messages = gateway.model.requests[0]
    assert isinstance(messages, list)
    assert len(messages) == 1
    message = messages[0]
    blocks = message.content
    assert blocks[0]["type"] == "image_url"
    assert blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert blocks[-1]["type"] == "text"
    assert "不访问任何链接" in blocks[-1]["text"]


def test_job_screenshot_extractor_rejects_non_job_even_when_text_looks_like_a_job() -> None:
    """视觉模型否定图片类型时，不能把它附带的职位样式文本交给后续解析器。"""

    gateway = RecordingGateway(
        """{
"is_job": false,
"confidence": 0.96,
"job_text": "Python 后端开发工程师\\n15-20K\\n杭州\\n任职要求：熟悉 FastAPI"
}"""
    )
    extractor = JobScreenshotExtractor(gateway)

    with pytest.raises(JobScreenshotError, match="未被可靠识别为招聘职位"):
        extractor.extract([JobScreenshot(content=png_bytes(), media_type="image/png")], account_id=7)


def test_job_screenshot_extractor_rejects_incomplete_job_text() -> None:
    """模型判断为职位也不够，截图必须包含足够的可比较字段。"""

    gateway = RecordingGateway(
        """{
"is_job": true,
"confidence": 0.96,
"job_text": "Python 后端开发工程师\\n杭州\\n岗位职责：负责 Python 后端开发。"
}"""
    )
    extractor = JobScreenshotExtractor(gateway)

    with pytest.raises(JobScreenshotError, match="职位截图信息不完整"):
        extractor.extract([JobScreenshot(content=png_bytes(), media_type="image/png")], account_id=7)


def test_job_screenshot_extractor_rejects_non_image_before_model_call() -> None:
    """伪装文件必须在本地校验阶段被拒绝，不能消耗模型调用。"""

    gateway = RecordingGateway("不会被使用")
    extractor = JobScreenshotExtractor(gateway)

    with pytest.raises(JobScreenshotError, match="有效的 PNG、JPEG 或 WebP"):
        extractor.extract([JobScreenshot(content=b"not an image", media_type="image/png")], account_id=7)

    assert gateway.model.requests == []


def test_web_imports_job_from_user_uploaded_screenshot(monkeypatch) -> None:
    """截图 API 应把用户上传内容交给应用门面，再返回现有职位格式。"""

    captured: dict[str, object] = {}

    def fake_import_screenshots(
        self: JobHuntingApp,
        screenshots: list[JobScreenshot],
        source_url: str | None = None,
        *,
        account_id: int | None = None,
    ):
        captured["screenshots"] = screenshots
        captured["source_url"] = source_url
        captured["account_id"] = account_id
        return self.import_job_text(
            """Python 后端开发工程师
15-20K
杭州
1-3年
本科
职位描述：负责 Python 和 FastAPI 后端开发。""",
            source_url,
            account_id=account_id,
            classify_with_llm=False,
            import_method="screenshot",
        )

    monkeypatch.setattr(JobHuntingApp, "import_job_screenshots", fake_import_screenshots, raising=False)
    client = TestClient(create_web_app())
    assert client.post(
        "/api/auth/register",
        json={"email": "screenshot-import@example.com", "password": "password-123"},
    ).status_code in {200, 409}
    assert client.post(
        "/api/auth/login",
        json={"email": "screenshot-import@example.com", "password": "password-123"},
    ).status_code == 200

    response = client.post(
        "/api/jobs/screenshots",
        data={"source_url": "https://www.zhipin.com/job_detail/example.html"},
        files=[("screenshots", ("boss-job.png", png_bytes(), "image/png"))],
    )

    assert response.status_code == 200, response.text
    assert response.json()["job"]["title"] == "Python 后端开发工程师"
    assert response.json()["job"]["import_method"] == "screenshot"
    assert response.json()["job"]["captured_at"]
    assert captured["source_url"] == "https://www.zhipin.com/job_detail/example.html"
    assert captured["account_id"] is not None
    uploaded = captured["screenshots"]
    assert isinstance(uploaded, list)
    assert uploaded[0].content == png_bytes()
    assert uploaded[0].media_type == "image/png"


def test_web_rejects_incomplete_job_screenshot(monkeypatch) -> None:
    """截图审核发现字段不足时，Web API 必须拒绝保存并返回可展示原因。"""

    def fake_import_screenshots(
        self: JobHuntingApp,
        screenshots: list[JobScreenshot],
        source_url: str | None = None,
        *,
        account_id: int | None = None,
    ):
        assert screenshots
        assert account_id is not None
        raise JobScreenshotError(
            "职位截图信息不完整，未保存任何职位信息。请上传包含职位名称和任职要求的完整截图。"
        )

    monkeypatch.setattr(JobHuntingApp, "import_job_screenshots", fake_import_screenshots, raising=False)
    client = TestClient(create_web_app())
    assert client.post(
        "/api/auth/register",
        json={"email": "screenshot-incomplete@example.com", "password": "password-123"},
    ).status_code in {200, 409}
    assert client.post(
        "/api/auth/login",
        json={"email": "screenshot-incomplete@example.com", "password": "password-123"},
    ).status_code == 200

    response = client.post(
        "/api/jobs/screenshots",
        files=[("screenshots", ("partial-job.png", png_bytes(), "image/png"))],
    )

    assert response.status_code == 400
    assert "职位截图信息不完整" in response.json()["detail"]


def test_web_rejects_duplicate_job_imported_from_screenshot(monkeypatch) -> None:
    """截图识别结果也必须进入同一份职位去重规则。"""

    def fake_import_screenshots(
        self: JobHuntingApp,
        screenshots: list[JobScreenshot],
        source_url: str | None = None,
        *,
        account_id: int | None = None,
    ):
        assert screenshots
        return self.import_job_text(
            """Python 后端开发工程师
15-20K
杭州
1-3年
本科
职位描述：负责 Python 和 FastAPI 后端开发。""",
            source_url,
            account_id=account_id,
            classify_with_llm=False,
            import_method="screenshot",
        )

    monkeypatch.setattr(JobHuntingApp, "import_job_screenshots", fake_import_screenshots, raising=False)
    client = TestClient(create_web_app())
    assert client.post(
        "/api/auth/register",
        json={"email": "screenshot-duplicate@example.com", "password": "password-123"},
    ).status_code == 200
    assert client.post(
        "/api/auth/login",
        json={"email": "screenshot-duplicate@example.com", "password": "password-123"},
    ).status_code == 200

    files = [("screenshots", ("boss-job.png", png_bytes(), "image/png"))]
    first = client.post("/api/jobs/screenshots", files=files)
    second = client.post("/api/jobs/screenshots", files=files)

    assert first.status_code == 200, first.text
    assert second.status_code == 409
    assert "职位信息" in second.json()["detail"]


def test_frontend_exposes_screenshot_job_import_mode() -> None:
    """职位导入工具应让用户发现截图入口，并使用 multipart API 上传文件。"""

    client = TestClient(create_web_app())

    home = client.get("/").text
    script = client.get("/static/app.js").text
    styles = client.get("/static/styles.css").text

    assert 'id="jobScreenshotFiles"' in home
    assert 'accept="image/png,image/jpeg,image/webp"' in home
    assert home.index('id="jobRawText"') < home.index('id="jobSourceUrl"')
    assert home.index('id="jobScreenshotFiles"') < home.index('id="jobSourceUrl"')
    assert "jobImportMode" in script
    assert 'requestFormJson("/api/jobs/screenshots", form)' in script
    assert "jobImportNotice" in home
    assert "showJobImportNotice" in script
    assert ".job-import-mode" in styles
