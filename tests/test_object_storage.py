"""MinIO/S3 对象存储边界测试。

测试使用内存假客户端，而不是连接真实 MinIO，确保单元测试可以独立验证
对象键、自动建桶和错误映射，不依赖 Docker 网络状态。
"""

from __future__ import annotations

from io import BytesIO

import pytest

from job_hunting_agent.config import load_object_storage_settings
from job_hunting_agent.object_storage import (
    ObjectNotFoundError,
    ObjectStorageError,
    S3ObjectStorage,
    validate_storage_key,
)


class FakeS3Error(RuntimeError):
    """模拟 boto3 携带 Error.Code 的异常结构。"""

    def __init__(self, code: str) -> None:
        """保存对象存储错误码，供生产适配器识别。"""

        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class FakeS3Client:
    """只实现本模块会调用的最小 S3 方法，避免测试访问网络。"""

    def __init__(self) -> None:
        """初始化 bucket、对象和最后一次写入参数的内存状态。"""

        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}
        self.last_put: dict[str, object] | None = None

    def head_bucket(self, *, Bucket: str) -> None:
        """模拟 bucket 不存在时的 S3 404 响应。"""

        if Bucket not in self.buckets:
            raise FakeS3Error("404")

    def create_bucket(self, *, Bucket: str) -> None:
        """记录新建 bucket。"""

        self.buckets.add(Bucket)

    def put_object(self, **kwargs: object) -> None:
        """保存对象正文，同时保留调用参数供断言 MIME 类型。"""

        bucket = str(kwargs["Bucket"])
        key = str(kwargs["Key"])
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        self.objects[(bucket, key)] = body
        self.last_put = kwargs

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        """返回可关闭的对象流，缺失时模拟 NoSuchKey。"""

        try:
            content = self.objects[(Bucket, Key)]
        except KeyError as error:
            raise FakeS3Error("NoSuchKey") from error
        return {"Body": BytesIO(content)}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        """模拟 S3 的幂等删除行为。"""

        self.objects.pop((Bucket, Key), None)


def test_s3_object_storage_creates_bucket_and_round_trips_resume_bytes() -> None:
    """对象首次写入会建桶，读取和删除都只使用数据库保存的对象键。"""

    client = FakeS3Client()
    storage = S3ObjectStorage(
        endpoint_url="http://minio:9000",
        bucket="job-agent-files",
        access_key="test-access-key",
        secret_key="test-secret-key",
        auto_create_bucket=True,
        client=client,
    )

    stored = storage.save(
        account_id=7,
        candidate_id=11,
        filename="candidate-resume.docx",
        content=b"resume-content",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert "job-agent-files" in client.buckets
    assert stored.storage_key.startswith("account-7/candidate-11/")
    assert stored.storage_key.endswith(".docx")
    assert storage.read(stored.storage_key) == b"resume-content"
    assert client.last_put is not None
    assert client.last_put["ContentType"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )

    storage.delete(stored.storage_key)
    with pytest.raises(ObjectNotFoundError):
        storage.read(stored.storage_key)


def test_s3_object_storage_builds_client_for_configured_minio_endpoint() -> None:
    """真实 boto3 客户端必须保留配置的 MinIO endpoint，而不是回退到 AWS 默认地址。"""

    storage = S3ObjectStorage(
        endpoint_url="http://minio:9000",
        bucket="job-agent-files",
        access_key="test-access-key",
        secret_key="test-secret-key",
    )

    assert storage.client.meta.endpoint_url == "http://minio:9000"


@pytest.mark.parametrize("storage_key", ["../secret.pdf", "/absolute/resume.pdf", "folder\\resume.pdf"])
def test_object_storage_rejects_unsafe_database_keys(storage_key: str) -> None:
    """数据库中的对象键即使被篡改，也不能变成任意文件或对象路径。"""

    with pytest.raises(ObjectStorageError):
        validate_storage_key(storage_key)


def test_object_storage_settings_load_minio_values_from_env_file(tmp_path) -> None:
    """MinIO 连接信息与模型配置一样只来自 `.env` 或系统环境。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_OBJECT_STORAGE_BACKEND=minio",
                "JOB_AGENT_OBJECT_STORAGE_ENDPOINT=http://127.0.0.1:9000",
                "JOB_AGENT_OBJECT_STORAGE_BUCKET=job-agent-files",
                "JOB_AGENT_OBJECT_STORAGE_ACCESS_KEY=local-access",
                "JOB_AGENT_OBJECT_STORAGE_SECRET_KEY=local-secret",
                "JOB_AGENT_OBJECT_STORAGE_REGION=us-east-1",
                "JOB_AGENT_OBJECT_STORAGE_FORCE_PATH_STYLE=true",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_object_storage_settings(env_file, environ={})

    assert settings.backend == "s3"
    assert settings.endpoint_url == "http://127.0.0.1:9000"
    assert settings.bucket == "job-agent-files"
    assert settings.access_key == "local-access"
    assert settings.secret_key == "local-secret"
    assert settings.force_path_style is True


def test_s3_settings_reject_missing_credentials(tmp_path) -> None:
    """启用对象存储后不能静默退回本地目录或匿名访问。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JOB_AGENT_OBJECT_STORAGE_BACKEND=s3",
                "JOB_AGENT_OBJECT_STORAGE_ENDPOINT=http://127.0.0.1:9000",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ACCESS_KEY"):
        load_object_storage_settings(env_file, environ={})
