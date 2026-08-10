"""简历文件读取、校验和受控文件存储。

该模块只处理文档本身，不判断候选人事实，也不调用 LLM：

- DOCX 从 OOXML 段落中提取文本，可覆盖普通段落、表格和文本框。
- PDF 优先读取文本层，缺少文本层的页面才调用本地 RapidOCR。
- 文件存储使用随机键和路径边界检查，用户文件名只作为下载名称保存。
"""

from __future__ import annotations

import hashlib
import os
import re
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree

import pdfplumber
from PIL import Image

from .object_storage import ObjectNotFoundError, ObjectStorageError, build_storage_key, validate_storage_key


DOCX_EXTENSION = ".docx"
PDF_EXTENSION = ".pdf"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MEDIA_TYPE = "application/pdf"
MAX_RESUME_FILE_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 30
MAX_DOCX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_DOCX_XML_PART_BYTES = 10 * 1024 * 1024
MIN_DOCUMENT_TEXT_CHARS = 20
MIN_PDF_TEXT_CHARS_PER_PAGE = 20
WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ResumeDocumentError(ValueError):
    """上传文件不受支持、损坏或无法提取有效文本。"""


@dataclass(frozen=True)
class ResumeExtraction:
    """从一份简历中提取出的可检索正文和解析元数据。"""

    text: str
    method: str
    page_count: int | None


@dataclass(frozen=True)
class PDFTextLayerInspection:
    """PDF 文本层检查结果，用于决定是否应把 OCR 交给后台 Worker。"""

    page_count: int
    pages_needing_ocr: list[int]


@dataclass(frozen=True)
class StoredResumeFile:
    """写入受控目录后的文件摘要。"""

    storage_key: str
    file_size: int
    sha256: str


OCRRunner = Callable[[Image.Image], str]


class ResumeFileStore:
    """把简历二进制文件限制在一个明确的存储根目录内。"""

    def __init__(self, root: str | Path):
        """记录并规范化文件根目录，真正写入时才创建子目录。"""

        self.root = Path(root).resolve()

    def save(
        self,
        *,
        account_id: int | None,
        candidate_id: int,
        filename: str,
        content: bytes,
        media_type: str | None = None,
    ) -> StoredResumeFile:
        """用随机存储键原子写入文件，避免同名覆盖和部分写入。"""

        # 本地测试实现不需要使用 MIME 类型；保留参数以满足对象存储统一接口。
        del media_type
        extension = supported_resume_extension(filename)
        storage_key = build_storage_key(
            account_id=account_id,
            candidate_id=candidate_id,
            filename=f"resume{extension}",
        )
        target = self.path_for(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, target)
        finally:
            # `os.replace` 成功后临时文件已经不存在；失败时清理残留片段。
            temporary.unlink(missing_ok=True)
        return StoredResumeFile(
            storage_key=storage_key,
            file_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def path_for(self, storage_key: str) -> Path:
        """解析数据库存储键，并拒绝任何逃逸文件根目录的路径。"""

        try:
            key = validate_storage_key(storage_key)
        except ObjectStorageError as error:
            raise ResumeDocumentError(str(error)) from error
        candidate = (self.root / Path(key)).resolve()
        if not candidate.is_relative_to(self.root):
            raise ResumeDocumentError("简历文件存储路径越过了允许目录。")
        return candidate

    def read(self, storage_key: str) -> bytes:
        """读取本地测试文件，并把缺失文件映射为统一对象存储异常。"""

        target = self.path_for(storage_key)
        if not target.is_file():
            raise ObjectNotFoundError("对象不存在。")
        return target.read_bytes()

    def stream(self, storage_key: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """分块读取本地测试文件，保持和 S3 下载接口一致。"""

        target = self.path_for(storage_key)
        if not target.is_file():
            raise ObjectNotFoundError("对象不存在。")
        if chunk_size <= 0:
            raise ValueError("对象流的分块大小必须大于 0。")

        def chunks() -> Iterator[bytes]:
            """在响应完成或中断时自动关闭本地文件句柄。"""

            with target.open("rb") as handle:
                while chunk := handle.read(chunk_size):
                    yield chunk

        return chunks()

    def delete(self, storage_key: str) -> None:
        """数据库写入失败时清理刚保存的孤立文件。"""

        self.path_for(storage_key).unlink(missing_ok=True)


def extract_resume_document(
    filename: str,
    content: bytes,
    *,
    ocr_runner: OCRRunner | None = None,
) -> ResumeExtraction:
    """根据扩展名和真实文件签名提取 DOCX/PDF 简历文本。"""

    extension = supported_resume_extension(filename)
    validate_resume_file_size(content)
    if extension == DOCX_EXTENSION:
        return extract_docx(content)
    return extract_pdf(content, ocr_runner=ocr_runner)


def supported_resume_extension(filename: str) -> str:
    """返回允许的规范扩展名，不接受伪装成简历的其他格式。"""

    extension = Path(filename or "").suffix.lower()
    if extension not in {DOCX_EXTENSION, PDF_EXTENSION}:
        raise ResumeDocumentError("仅支持上传 .docx 或 .pdf 简历文件。")
    return extension


def validate_resume_file_size(content: bytes) -> None:
    """阻止空文件和超出本地 MVP 限额的大文件。"""

    if not content:
        raise ResumeDocumentError("简历文件不能为空。")
    if len(content) > MAX_RESUME_FILE_BYTES:
        raise ResumeDocumentError("简历文件不能超过 20 MB。")


def media_type_for_filename(filename: str) -> str:
    """根据已经校验过的扩展名返回稳定 MIME 类型。"""

    return DOCX_MEDIA_TYPE if supported_resume_extension(filename) == DOCX_EXTENSION else PDF_MEDIA_TYPE


def sanitize_download_filename(filename: str, fallback: str = "resume") -> str:
    """清理下载名称中的路径和 Windows 非法字符，保留中文可读性。"""

    basename = Path(filename or "").name.strip()
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", basename).strip(" .")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) <= 120:
        return cleaned

    # 截断超长源文件名时保留扩展名，否则合法 DOCX/PDF 会在后续签名检查前被误拒绝。
    suffix = Path(cleaned).suffix
    if suffix and len(suffix) < 120:
        stem = cleaned[: -len(suffix)][: 120 - len(suffix)].rstrip(" .")
        return f"{stem or fallback}{suffix}"
    return cleaned[:120]


def extract_docx(content: bytes) -> ResumeExtraction:
    """从 OOXML 段落提取 DOCX 文本，并校验必要的 Word 部件。"""

    if not content.startswith(b"PK"):
        raise ResumeDocumentError("DOCX 文件签名无效，文件可能已损坏或扩展名不正确。")
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise ResumeDocumentError("上传文件不是有效的 Word DOCX 文档。")
            part_names = ["word/document.xml"]
            part_names.extend(
                name
                for name in sorted(names)
                if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
            )
            validate_docx_archive_sizes(archive, part_names)
            lines: list[str] = []
            for part_name in part_names:
                lines.extend(extract_ooxml_paragraphs(archive.read(part_name)))
    except ResumeDocumentError:
        raise
    except (zipfile.BadZipFile, ElementTree.ParseError, OSError) as error:
        raise ResumeDocumentError(f"无法读取 DOCX 简历：{error}") from error

    text = normalize_extracted_text("\n".join(lines))
    ensure_meaningful_resume_text(text)
    return ResumeExtraction(text=text, method="docx", page_count=None)


def validate_docx_archive_sizes(archive: zipfile.ZipFile, part_names: list[str]) -> None:
    """在解压 XML 前拒绝异常膨胀的 DOCX，降低压缩炸弹造成的内存风险。"""

    if sum(info.file_size for info in archive.infolist()) > MAX_DOCX_UNCOMPRESSED_BYTES:
        raise ResumeDocumentError("DOCX 解压后内容过大，请压缩图片或精简文档后重试。")
    for part_name in part_names:
        if archive.getinfo(part_name).file_size > MAX_DOCX_XML_PART_BYTES:
            raise ResumeDocumentError("DOCX 正文结构异常过大，无法安全读取。")


def extract_ooxml_paragraphs(xml_content: bytes) -> list[str]:
    """按 Word 段落顺序读取文本节点，表格和文本框也能被覆盖。"""

    root = ElementTree.fromstring(xml_content)
    lines: list[str] = []
    for paragraph in root.iter(f"{WORD_NAMESPACE}p"):
        text = "".join(
            node.text or ""
            for node in paragraph.iter(f"{WORD_NAMESPACE}t")
        ).strip()
        if text and (not lines or lines[-1] != text):
            lines.append(text)
    return lines


def extract_pdf(content: bytes, *, ocr_runner: OCRRunner | None = None) -> ResumeExtraction:
    """逐页读取 PDF 文本层，只对文本不足的页面执行 OCR。"""

    page_count, page_texts = _read_pdf_text_layers(content)
    pages_needing_ocr = [
        index
        for index, text in enumerate(page_texts)
        if len(text) < MIN_PDF_TEXT_CHARS_PER_PAGE
    ]
    if pages_needing_ocr:
        runner = ocr_runner or run_rapidocr
        ocr_texts = render_and_ocr_pdf_pages(content, pages_needing_ocr, runner)
        for index, text in ocr_texts.items():
            page_texts[index] = normalize_extracted_text(text)

    text = normalize_extracted_text("\n\n".join(page_texts))
    ensure_meaningful_resume_text(text)
    if not pages_needing_ocr:
        method = "pdf_text"
    elif len(pages_needing_ocr) == page_count:
        method = "pdf_ocr"
    else:
        method = "pdf_mixed"
    return ResumeExtraction(text=text, method=method, page_count=page_count)


def inspect_pdf_for_ocr(content: bytes) -> PDFTextLayerInspection:
    """只检查 PDF 文本层，不渲染页面也不加载 RapidOCR 模型。"""

    validate_resume_file_size(content)
    page_count, page_texts = _read_pdf_text_layers(content)
    return PDFTextLayerInspection(
        page_count=page_count,
        pages_needing_ocr=[
            index
            for index, text in enumerate(page_texts)
            if len(text) < MIN_PDF_TEXT_CHARS_PER_PAGE
        ],
    )


def _read_pdf_text_layers(content: bytes) -> tuple[int, list[str]]:
    """校验 PDF 并读取每页文字层，供同步解析和异步任务判定共用。"""

    if not content.startswith(b"%PDF-"):
        raise ResumeDocumentError("PDF 文件签名无效，文件可能已损坏或扩展名不正确。")
    try:
        with pdfplumber.open(BytesIO(content)) as pdf:
            page_count = len(pdf.pages)
            if page_count == 0:
                raise ResumeDocumentError("PDF 中没有可读取的页面。")
            if page_count > MAX_PDF_PAGES:
                raise ResumeDocumentError(f"PDF 页数不能超过 {MAX_PDF_PAGES} 页。")
            page_texts = [normalize_extracted_text(page.extract_text() or "") for page in pdf.pages]
    except ResumeDocumentError:
        raise
    except Exception as error:  # noqa: BLE001 - 第三方 PDF 解析器异常统一转成用户可读错误。
        raise ResumeDocumentError(f"无法读取 PDF 简历：{error}") from error
    return page_count, page_texts


def render_and_ocr_pdf_pages(
    content: bytes,
    page_indexes: list[int],
    ocr_runner: OCRRunner,
) -> dict[int, str]:
    """使用 PDFium 把指定页渲染成图片，再交给可替换 OCR 运行器。"""

    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(content)
        results: dict[int, str] = {}
        for index in page_indexes:
            page = document[index]
            bitmap = page.render(scale=2.0)
            image = bitmap.to_pil().convert("RGB")
            results[index] = ocr_runner(image)
            image.close()
            bitmap.close()
            page.close()
        document.close()
        return results
    except ResumeDocumentError:
        raise
    except Exception as error:  # noqa: BLE001 - OCR 依赖异常统一映射为上传错误。
        raise ResumeDocumentError(f"扫描版 PDF OCR 失败：{error}") from error


def run_rapidocr(image: Image.Image) -> str:
    """惰性加载本地 RapidOCR，避免文字版 PDF 也承担模型初始化开销。"""

    try:
        from rapidocr import RapidOCR

        result = RapidOCR()(image)
    except Exception as error:  # noqa: BLE001 - 模型或 ONNX 初始化问题需要返回明确原因。
        raise ResumeDocumentError(f"本地 OCR 不可用：{error}") from error
    texts = getattr(result, "txts", None) or ()
    return "\n".join(str(text).strip() for text in texts if str(text).strip())


def normalize_extracted_text(text: str) -> str:
    """去除控制字符和多余空白，同时保留简历段落边界。"""

    normalized_lines: list[str] = []
    previous_blank = False
    for raw_line in text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"[\t\u00a0 ]+", " ", raw_line).strip()
        if line:
            normalized_lines.append(line)
            previous_blank = False
        elif normalized_lines and not previous_blank:
            normalized_lines.append("")
            previous_blank = True
    return "\n".join(normalized_lines).strip()


def ensure_meaningful_resume_text(text: str) -> None:
    """没有提取到足够文本时拒绝保存，避免空扫描件进入知识库。"""

    if len(re.sub(r"\s+", "", text)) < MIN_DOCUMENT_TEXT_CHARS:
        raise ResumeDocumentError("没有从简历中识别出足够文字，请确认文件清晰且未加密。")
