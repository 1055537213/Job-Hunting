"""对象存储边界。

文件正文属于二进制对象，不应和 PostgreSQL 中的结构化事实混在一起。
本模块定义统一接口，并提供面向本地 MinIO/S3 的实现；业务层不直接依赖
某一个对象存储厂商的 SDK。
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import uuid4


class ObjectStorageError(RuntimeError):
    """对象存储不可用或操作失败。"""


class ObjectNotFoundError(ObjectStorageError):
    """请求的对象不存在。"""


@dataclass(frozen=True)
class StoredObject:
    """一次对象写入后返回给业务层的摘要。"""

    storage_key: str
    file_size: int
    sha256: str


class ObjectStorage(Protocol):
    """简历文件和其他受控二进制对象使用的最小存储协议。"""

    def save(
        self,
        *,
        account_id: int | None,
        candidate_id: int,
        filename: str,
        content: bytes,
        media_type: str | None = None,
    ) -> StoredObject:
        """写入对象并返回稳定的对象键和校验摘要。"""

    def read(self, storage_key: str) -> bytes:
        """读取对象正文。"""

    def stream(self, storage_key: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """以固定大小分块读取对象正文，避免下载占满 Web 进程内存。"""

    def delete(self, storage_key: str) -> None:
        """删除对象；重复删除应保持幂等。"""


def build_storage_key(
    *,
    account_id: int | None,
    candidate_id: int,
    filename: str,
) -> str:
    """生成不包含用户原始文件名的对象键，避免同名覆盖和路径注入。"""

    suffix = Path(filename or "").suffix.lower()
    owner_segment = f"account-{account_id}" if account_id is not None else "account-legacy"
    candidate_segment = f"candidate-{candidate_id}"
    return (
        PurePosixPath(owner_segment)
        / candidate_segment
        / f"{uuid4().hex}{suffix}"
    ).as_posix()


def validate_storage_key(storage_key: str) -> str:
    """校验数据库中的对象键，拒绝绝对路径、回退路径和 Windows 分隔符。"""

    if not storage_key or "\\" in storage_key:
        raise ObjectStorageError("对象键格式无效。")
    path = PurePosixPath(storage_key)
    if path.is_absolute() or ".." in path.parts:
        raise ObjectStorageError("对象键不能越过存储根目录。")
    return path.as_posix()


class S3ObjectStorage:
    """通过 S3 API 访问 MinIO 或其他 S3-compatible 对象存储。"""

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        force_path_style: bool = True,
        auto_create_bucket: bool = False,
        client: Any | None = None,
    ) -> None:
        """保存连接参数；传入 client 主要用于不依赖网络的单元测试。"""

        if not endpoint_url.strip():
            raise ObjectStorageError("对象存储 endpoint 不能为空。")
        if not bucket.strip():
            raise ObjectStorageError("对象存储 bucket 不能为空。")
        if not access_key or not secret_key:
            raise ObjectStorageError("S3-compatible 对象存储必须配置访问凭证。")

        self.endpoint_url = endpoint_url.rstrip("/")
        self.bucket = bucket.strip()
        self.region = region
        self.auto_create_bucket = auto_create_bucket
        self._bucket_ready = False
        self._bucket_lock = threading.Lock()
        self.client = client or self._build_client(
            endpoint_url=self.endpoint_url,
            access_key=access_key,
            secret_key=secret_key,
            region=region,
            force_path_style=force_path_style,
        )

    @staticmethod
    def _build_client(
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str,
        force_path_style: bool,
    ) -> Any:
        """惰性加载 boto3，避免本地纯规则测试必须初始化云端 SDK。"""

        try:
            import boto3
            from botocore.client import Config
        except ModuleNotFoundError as error:
            raise ObjectStorageError(
                "S3 对象存储需要 boto3，请先安装项目依赖。"
            ) from error

        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if force_path_style else "auto"},
        )
        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=config,
        )

    @staticmethod
    def _error_code(error: Exception) -> str:
        """从 boto3 异常中提取短错误码，不记录请求正文或密钥。"""

        response = getattr(error, "response", None)
        if isinstance(response, Mapping):
            details = response.get("Error")
            if isinstance(details, Mapping):
                return str(details.get("Code") or "")
        return ""

    def _ensure_bucket(self) -> None:
        """首次写入前创建开发 bucket；生产环境也允许预先创建好的 bucket。"""

        if self._bucket_ready:
            return
        with self._bucket_lock:
            if self._bucket_ready:
                return
            try:
                self.client.head_bucket(Bucket=self.bucket)
            except Exception as error:  # noqa: BLE001 - SDK 异常需统一映射
                if self._error_code(error) not in {"404", "NoSuchBucket", "NotFound"}:
                    raise ObjectStorageError("对象存储 bucket 检查失败。") from error
                if not self.auto_create_bucket:
                    raise ObjectStorageError(
                        "对象存储 bucket 不存在，请由部署环境预先创建。"
                    ) from error
                try:
                    create_kwargs: dict[str, object] = {"Bucket": self.bucket}
                    # MinIO 不需要区域参数；AWS 非 us-east-1 区域则要求显式声明。
                    if self.region and self.region != "us-east-1":
                        create_kwargs["CreateBucketConfiguration"] = {
                            "LocationConstraint": self.region
                        }
                    self.client.create_bucket(**create_kwargs)
                except Exception as create_error:  # noqa: BLE001 - 处理并发建桶
                    if self._error_code(create_error) not in {
                        "BucketAlreadyOwnedByYou",
                        "BucketAlreadyExists",
                    }:
                        raise ObjectStorageError("对象存储 bucket 创建失败。") from create_error
            self._bucket_ready = True

    def health_check(self) -> None:
        """检查 bucket 是否可访问，供启动检查和运维探针调用。"""

        self._ensure_bucket()

    def save(
        self,
        *,
        account_id: int | None,
        candidate_id: int,
        filename: str,
        content: bytes,
        media_type: str | None = None,
    ) -> StoredObject:
        """以一次 PutObject 写入对象，并返回业务层所需的校验摘要。"""

        storage_key = build_storage_key(
            account_id=account_id,
            candidate_id=candidate_id,
            filename=filename,
        )
        self._ensure_bucket()
        try:
            kwargs: dict[str, object] = {
                "Bucket": self.bucket,
                "Key": validate_storage_key(storage_key),
                "Body": content,
            }
            if media_type:
                kwargs["ContentType"] = media_type
            self.client.put_object(**kwargs)
        except Exception as error:  # noqa: BLE001 - SDK 异常需统一映射
            raise ObjectStorageError("对象存储写入失败。") from error
        return StoredObject(
            storage_key=storage_key,
            file_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def read(self, storage_key: str) -> bytes:
        """读取完整对象正文，供解析器和小文件校验使用。"""

        return b"".join(self.stream(storage_key))

    def stream(self, storage_key: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """打开 S3 响应流并返回分块迭代器，使 Web 可以真正流式下载。"""

        key = validate_storage_key(storage_key)
        if chunk_size <= 0:
            raise ValueError("对象流的分块大小必须大于 0。")
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as error:  # noqa: BLE001 - SDK 异常需统一映射
            if self._error_code(error) in {"404", "NoSuchKey", "NoSuchObject", "NotFound"}:
                raise ObjectNotFoundError("对象不存在。") from error
            raise ObjectStorageError("对象存储读取失败。") from error
        body = response.get("Body")
        if body is None or not hasattr(body, "read"):
            raise ObjectStorageError("对象存储返回了无效的对象正文。")

        def chunks() -> Iterator[bytes]:
            """在迭代结束或客户端断开时关闭底层 HTTP 响应。"""

            try:
                while chunk := body.read(chunk_size):
                    yield bytes(chunk)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()

        return chunks()

    def delete(self, storage_key: str) -> None:
        """删除对象；S3 的 DeleteObject 本身就是幂等操作。"""

        key = validate_storage_key(storage_key)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as error:  # noqa: BLE001 - SDK 异常需统一映射
            raise ObjectStorageError("对象存储删除失败。") from error
