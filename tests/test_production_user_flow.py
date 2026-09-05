from __future__ import annotations

from argparse import Namespace

import pytest

from scripts.validate_production_user_flow import (
    _http_detail,
    _mapping_value,
    _sequence_value,
    run,
)


def runner_args(*, base_url: str, confirmation: str) -> Namespace:
    return Namespace(
        base_url=base_url,
        confirmation=confirmation,
        env_file="/app/.env",
        timeout_seconds=180.0,
    )


def test_production_user_flow_requires_explicit_confirmation() -> None:
    with pytest.raises(ValueError, match="RUN_PRODUCTION_USER_FLOW"):
        run(runner_args(base_url="https://example.com", confirmation="RUN"))


def test_production_user_flow_requires_https() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        run(
            runner_args(
                base_url="http://127.0.0.1:8000",
                confirmation="RUN_PRODUCTION_USER_FLOW",
            )
        )


def test_production_user_flow_response_helpers_reject_wrong_shapes() -> None:
    assert _mapping_value({"billing": {"balance": 1}}, "billing") == {"balance": 1}
    assert _mapping_value({"billing": []}, "billing") == {}
    assert _sequence_value({"items": [{"id": 1}, "invalid"]}, "items") == [
        {"id": 1}
    ]
    assert _sequence_value({"items": "invalid"}, "items") == []
    assert _http_detail(200, {"large": "body"}) == "HTTP 200"
    assert _http_detail(400, {"detail": "bad"}) == "HTTP 400: {'detail': 'bad'}"
