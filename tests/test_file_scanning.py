"""Unit coverage for the ClamAV streaming protocol boundary."""

from __future__ import annotations

import struct

import pytest

from job_hunting_agent.file_scanning import (
    ClamAVScanner,
    FileInfectedError,
    FileScannerUnavailableError,
)


class FakeClamAVConnection:
    """Minimal socket double that records the INSTREAM request."""

    def __init__(self, response: bytes):
        self.response = response
        self.sent = bytearray()
        self.timeout: float | None = None

    def __enter__(self) -> FakeClamAVConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def sendall(self, content: bytes) -> None:
        self.sent.extend(content)

    def recv(self, size: int) -> bytes:
        chunk = self.response[:size]
        self.response = self.response[size:]
        return chunk


def test_clamav_scanner_sends_instream_frames_and_accepts_clean_content(monkeypatch) -> None:
    connection = FakeClamAVConnection(b"stream: OK\0")
    monkeypatch.setattr(
        "job_hunting_agent.file_scanning.socket.create_connection",
        lambda *args, **kwargs: connection,
    )

    result = ClamAVScanner("clamav", 3310, timeout_seconds=3).scan(
        "resume.docx",
        b"clean",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert result.status == "clean"
    assert result.engine == "clamav"
    assert connection.timeout == 3
    assert bytes(connection.sent) == (
        b"zINSTREAM\0" + struct.pack("!I", 5) + b"clean" + b"\0\0\0\0"
    )


def test_clamav_scanner_maps_found_response_to_infected_error(monkeypatch) -> None:
    connection = FakeClamAVConnection(b"stream: Win.Test.EICAR_HDB-1 FOUND\n")
    monkeypatch.setattr(
        "job_hunting_agent.file_scanning.socket.create_connection",
        lambda *args, **kwargs: connection,
    )

    with pytest.raises(FileInfectedError, match="未通过安全扫描"):
        ClamAVScanner("clamav", 3310).scan("eicar.txt", b"eicar")


@pytest.mark.parametrize(
    "response",
    (
        b"stream: INSTREAM size limit exceeded. ERROR\0",
        b"unexpected response\0",
    ),
)
def test_clamav_scanner_fails_closed_on_non_verdict_responses(monkeypatch, response) -> None:
    connection = FakeClamAVConnection(response)
    monkeypatch.setattr(
        "job_hunting_agent.file_scanning.socket.create_connection",
        lambda *args, **kwargs: connection,
    )

    with pytest.raises(FileScannerUnavailableError, match="无法识别"):
        ClamAVScanner("clamav", 3310).scan("resume.docx", b"content")


def test_clamav_scanner_fails_closed_when_connection_is_unavailable(monkeypatch) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise ConnectionRefusedError("clamd is down")

    monkeypatch.setattr(
        "job_hunting_agent.file_scanning.socket.create_connection",
        unavailable,
    )

    with pytest.raises(FileScannerUnavailableError, match="暂时不可用"):
        ClamAVScanner("clamav", 3310).scan("resume.docx", b"content")
