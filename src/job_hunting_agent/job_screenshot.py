"""用户主动上传的职位截图转写。

这个模块只负责把短生命周期的截图交给已配置的多模态模型，取得可审核的职位原文。
它不会访问截图旁填写的网址，也不会保存截图；提取出的文本仍由既有职位解析、去重和
持久化流程处理。
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from PIL import Image, UnidentifiedImageError

from .job_parser import validate_job_text
from .concurrency_control import ConcurrencyControlError
from .llm import LLMRequestError, extract_message_text
from .model_resilience import ModelCircuitOpenError

logger = logging.getLogger(__name__)

MAX_JOB_SCREENSHOT_FILES = 4
MAX_JOB_SCREENSHOT_FILE_BYTES = 8 * 1024 * 1024
MAX_JOB_SCREENSHOT_TOTAL_BYTES = 20 * 1024 * 1024
MAX_JOB_SCREENSHOT_PIXELS = 24_000_000
SUPPORTED_IMAGE_FORMATS = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}

# 截图只能看到职位页面的一部分，不能像粘贴全文一样容忍大量字段缺失。候选人至少
# 应能据此判断岗位方向和基本条件，因此除职位名称与职责/要求外，还要求两个背景字段。
SCREENSHOT_CONTEXT_SIGNALS = frozenset(
    {
        "薪资",
        "工作地点",
        "经验要求",
        "学历要求",
        "公司信息",
    }
)


class JobScreenshotError(ValueError):
    """职位截图上传、校验或识别无法继续时抛出。"""


class JobScreenshotModelError(JobScreenshotError):
    """多模态模型未配置、不可用或未返回文本时抛出。"""


@dataclass(frozen=True)
class JobScreenshot:
    """一个经 Web 上传获得的职位页面截图。"""

    content: bytes
    media_type: str | None = None


class ScreenshotModelGateway(Protocol):
    """截图转写所需的最小模型网关接口。"""

    def new_call_context(self, operation: str, *, account_id: int | None = None) -> Any:
        """创建可计量的模型调用上下文。"""

    def chat_model(
        self,
        operation: str,
        temperature: float = 0,
        *,
        account_id: int | None = None,
    ) -> BaseChatModel:
        """返回指定操作使用的聊天模型。"""

    def record_chat_response(self, context: Any, response: BaseMessage | object) -> object:
        """记录模型返回的用量信息。"""


class JobScreenshotExtractor:
    """从用户截图中转写职位原文的深模块。

    Web 层只需要传入上传文件和账号 ID；图片格式、大小、提示词、base64 消息结构和
    用量回调都收敛在这里，避免路由层了解供应商多模态协议。
    """

    def __init__(self, model_gateway: ScreenshotModelGateway):
        """绑定项目内唯一允许直连模型的 Gateway。"""

        self.model_gateway = model_gateway

    def extract(
        self,
        screenshots: Sequence[JobScreenshot],
        *,
        account_id: int | None,
    ) -> str:
        """校验截图并返回模型可见内容对应的纯文本职位原文。"""

        normalized = validate_job_screenshots(screenshots)
        context = self.model_gateway.new_call_context(
            "job_screenshot_extraction",
            account_id=account_id,
        )
        try:
            response = self.model_gateway.chat_model(
                "job_screenshot_extraction",
                temperature=0,
                account_id=context.account_id,
            ).invoke([build_screenshot_message(normalized)])
        except (ConcurrencyControlError, ModelCircuitOpenError):
            raise
        except Exception as error:
            raise JobScreenshotModelError(
                "职位截图识别失败，请检查当前模型是否支持图片输入，或改用文本导入。"
            ) from error

        # 用量流水是旁路信息，不能覆盖已经成功得到的识别结果。
        try:
            self.model_gateway.record_chat_response(context, response)
        except Exception as error:  # noqa: BLE001 - 计量是旁路，不能阻断已成功的导入。
            # 只记录异常类型，避免把供应商响应或用户截图相关内容写入日志。
            logger.debug("职位截图模型用量记录失败：%s", type(error).__name__)

        try:
            text = parse_screenshot_extraction(extract_message_text(response))
        except LLMRequestError as error:
            raise JobScreenshotModelError(
                "职位截图识别没有返回可用文本，请换一张更清晰的截图或改用文本导入。"
            ) from error
        return ensure_complete_screenshot_job_text(text)


def validate_job_screenshots(screenshots: Sequence[JobScreenshot]) -> list[JobScreenshot]:
    """校验数量、体积、真实格式和像素上限，并返回规范 MIME 类型的截图。"""

    if not screenshots:
        raise JobScreenshotError("请至少选择一张职位截图。")
    if len(screenshots) > MAX_JOB_SCREENSHOT_FILES:
        raise JobScreenshotError(f"一次最多上传 {MAX_JOB_SCREENSHOT_FILES} 张职位截图。")

    total_bytes = 0
    normalized: list[JobScreenshot] = []
    for screenshot in screenshots:
        content = screenshot.content
        if not content:
            raise JobScreenshotError("职位截图不能为空。")
        if len(content) > MAX_JOB_SCREENSHOT_FILE_BYTES:
            raise JobScreenshotError("单张职位截图不能超过 8 MB。")
        total_bytes += len(content)
        if total_bytes > MAX_JOB_SCREENSHOT_TOTAL_BYTES:
            raise JobScreenshotError("职位截图总大小不能超过 20 MB。")

        media_type, width, height = inspect_job_screenshot(content)
        if width * height > MAX_JOB_SCREENSHOT_PIXELS:
            raise JobScreenshotError("职位截图像素过大，请压缩后重试。")
        normalized.append(JobScreenshot(content=content, media_type=media_type))
    return normalized


def inspect_job_screenshot(content: bytes) -> tuple[str, int, int]:
    """用 Pillow 检查真实图片格式，避免只信任浏览器提交的 MIME 声明。"""

    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
        with Image.open(BytesIO(content)) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError) as error:
        raise JobScreenshotError("上传内容不是有效的 PNG、JPEG 或 WebP 职位截图。") from error

    media_type = SUPPORTED_IMAGE_FORMATS.get(image_format)
    if media_type is None:
        raise JobScreenshotError("仅支持上传 PNG、JPEG 或 WebP 职位截图。")
    if width <= 0 or height <= 0:
        raise JobScreenshotError("职位截图尺寸无效。")
    return media_type, width, height


def build_screenshot_message(screenshots: Sequence[JobScreenshot]) -> HumanMessage:
    """构造供应商兼容的图片 data URL 消息，不传递来源链接。"""

    # LangChain 的消息块允许不同形状的 provider payload；这里显式使用 Any 仅限于
    # OpenAI-compatible `image_url` 协议边界，图片字节仍已在上方完成本地校验。
    content: list[str | dict[Any, Any]] = []
    for screenshot in screenshots:
        assert screenshot.media_type is not None
        encoded = base64.b64encode(screenshot.content).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{screenshot.media_type};base64,{encoded}",
                    "detail": "high",
                },
            }
        )
    content.append({"type": "text", "text": JOB_SCREENSHOT_EXTRACTION_PROMPT})
    return HumanMessage(content=content)


def parse_screenshot_extraction(text: str) -> str:
    """校验视觉模型的职位判断和转写内容，拒绝无法证明为职位的图片。

    模型的视觉判断不是唯一防线：成功返回后仍会由 ``job_parser`` 审核职位文本。
    这里要求模型先显式声明 ``is_job``，避免把任意截图转写成看似完整的职位后直接入库。
    """

    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    try:
        decision = json.loads(stripped)
    except json.JSONDecodeError as error:
        raise JobScreenshotModelError(
            "职位截图识别结果格式异常，请换一张更清晰的职位截图或改用文本导入。"
        ) from error
    if not isinstance(decision, dict):
        raise JobScreenshotModelError("职位截图识别结果格式异常，请改用文本导入。")

    is_job = decision.get("is_job")
    confidence = decision.get("confidence")
    job_text = decision.get("job_text")
    if not isinstance(is_job, bool) or not isinstance(confidence, (int, float)):
        raise JobScreenshotModelError("职位截图识别结果缺少可靠的职位判断，请改用文本导入。")
    if not 0 <= float(confidence) <= 1:
        raise JobScreenshotModelError("职位截图识别置信度无效，请改用文本导入。")
    # 视觉模型明确否定或自己判断不够可靠时，绝不把它产生的文字交给职位解析器。
    if not is_job or float(confidence) < 0.8:
        raise JobScreenshotError("该图片未被可靠识别为招聘职位，未保存任何职位信息。")
    if not isinstance(job_text, str) or not job_text.strip():
        raise JobScreenshotModelError("职位截图识别没有返回可用职位文本，请改用文本导入。")
    return job_text.strip()


def ensure_complete_screenshot_job_text(job_text: str) -> str:
    """拒绝只有局部字段的职位截图，避免把不完整信息写入职位池。

    ``validate_job_text`` 是文本和截图共用的基础审核。截图是视觉模型从有限画面中
    转写得到的内容，因此在基础审核通过后额外要求“职责/要求描述”和两个背景字段，
    防止一张只露出标题或岗位卡片的图片被当作完整职位保存。
    """

    validation = validate_job_text(job_text)
    matched = set(validation.matched_signals)
    context_count = len(matched & SCREENSHOT_CONTEXT_SIGNALS)

    if (
        not validation.is_valid
        or "职位名称" not in matched
        or "职责/要求描述" not in matched
        or context_count < 2
    ):
        raise JobScreenshotError(
            "职位截图信息不完整，未保存任何职位信息。请上传包含职位名称、岗位职责或任职要求，"
            "以及薪资、地点、经验、学历、公司信息中至少两项的完整截图。"
        )
    return job_text


JOB_SCREENSHOT_EXTRACTION_PROMPT = """你是职位截图审核与转写器。请先判断用户主动上传的图片是否清楚展示了一个具体招聘职位，再转写可见信息。

输出规则：
- 只输出截图中可见的信息；不访问任何链接、不使用外部知识、不猜测模糊或被遮挡的内容。
- 只返回一个 JSON 对象，不要 Markdown 代码围栏或其他解释：
  {"is_job":true,"confidence":0.95,"job_text":"职位名称\\n薪资\\n城市\\n任职要求"}
- `is_job` 仅在截图可清楚证明是一个具体的招聘职位时为 true；聊天截图、项目页面、文章、广告、空白页或信息不足的图片必须为 false。
- 一张可导入的截图必须同时展示职位名称、岗位职责或任职要求，并至少展示薪资、地点、经验、学历、公司信息中的两项；不满足时 `is_job` 必须为 false。
- `confidence` 必须是 0 到 1 的数字；无法可靠判断时小于 0.8。
- `job_text` 只能包含可见的职位名称、薪资、城市、经验、学历、岗位职责、任职要求、公司等文字；多张同一职位截图可合并重复段落，但不得补写截图未展示的内容。
- 当 `is_job` 为 false 时，`job_text` 返回空字符串，不得编造职位信息。"""
