"""上传文件的恶意内容扫描边界。

扫描器不负责判断文件是不是合法的简历或图片；格式、大小和解压安全仍由各领域
解析器负责。它只负责在文件进入 OCR、模型或 RAG 之前给出 clean/infected 结论。
生产环境使用 ClamAV 的 clamd 协议，开发和测试使用本地安全检查，避免把病毒库
作为 Python 依赖打进业务镜像。
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from typing import Protocol


class FileScanError(RuntimeError):
    """文件扫描无法完成。"""


class FileInfectedError(FileScanError):
    """扫描器发现文件包含恶意内容。"""


class FileScannerUnavailableError(FileScanError):
    """扫描服务不可用，生产环境必须阻止文件继续处理。"""


@dataclass(frozen=True)
class FileScanResult:
    """一次扫描的低敏结果，不保存文件正文。"""

    status: str
    engine: str
    signature: str | None = None


class FileScanner(Protocol):
    """业务层依赖的最小扫描接口。"""

    engine: str

    def scan(self, filename: str, content: bytes, media_type: str | None = None) -> FileScanResult:
        """扫描文件；感染或扫描服务异常必须抛出明确异常。"""


class LocalSafetyScanner:
    """开发/测试使用的轻量安全检查。

    这不是病毒库，不能替代生产 ClamAV。保留 EICAR 识别是为了让测试和上线验收
    能验证“扫描拒绝后不进入下游流程”的状态机。
    """

    engine = "local-safety"
    EICAR_MARKER = b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE"

    def scan(
        self,
        filename: str,
        content: bytes,
        media_type: str | None = None,
    ) -> FileScanResult:
        del filename, media_type
        if self.EICAR_MARKER in content:
            raise FileInfectedError("上传文件未通过安全扫描。")
        return FileScanResult(status="clean", engine=self.engine)


class ClamAVScanner:
    """通过 clamd INSTREAM 协议扫描内存中的上传文件。"""

    def __init__(self, host: str, port: int, timeout_seconds: float = 10.0):
        if not host.strip():
            raise ValueError("ClamAV 主机不能为空。")
        if not 1 <= port <= 65535:
            raise ValueError("ClamAV 端口必须在 1 到 65535 之间。")
        if timeout_seconds <= 0:
            raise ValueError("ClamAV 超时时间必须大于 0。")
        self.host = host.strip()
        self.port = port
        self.timeout_seconds = timeout_seconds
        self.engine = "clamav"

    def scan(
        self,
        filename: str,
        content: bytes,
        media_type: str | None = None,
    ) -> FileScanResult:
        del filename, media_type
        try:
            with socket.create_connection(
                (self.host, self.port),
                timeout=self.timeout_seconds,
            ) as connection:
                connection.settimeout(self.timeout_seconds)
                connection.sendall(b"zINSTREAM\0")
                for start in range(0, len(content), 64 * 1024):
                    chunk = content[start : start + 64 * 1024]
                    connection.sendall(struct.pack("!I", len(chunk)) + chunk)
                connection.sendall(b"\x00\x00\x00\x00")
                response = _read_clamd_response(connection)
        except (OSError, TimeoutError) as error:
            raise FileScannerUnavailableError("文件安全扫描服务暂时不可用。") from error

        if response.endswith("FOUND"):
            raise FileInfectedError("上传文件未通过安全扫描。")
        if response.endswith("OK"):
            return FileScanResult(status="clean", engine=self.engine)
        raise FileScannerUnavailableError("文件安全扫描服务返回了无法识别的结果。")


def _read_clamd_response(connection: socket.socket) -> str:
    """读取 clamd 的单行响应，并限制响应大小。"""

    chunks: list[bytes] = []
    received = 0
    while received < 4096:
        chunk = connection.recv(min(512, 4096 - received))
        if not chunk:
            break
        chunks.append(chunk)
        received += len(chunk)
        if b"\n" in chunk or b"\x00" in chunk:
            break
    try:
        return b"".join(chunks).replace(b"\x00", b"").decode("utf-8", errors="replace").strip()
    except UnicodeError as error:
        raise FileScannerUnavailableError("文件安全扫描服务返回了无效结果。") from error
