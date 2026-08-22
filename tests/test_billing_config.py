import pytest

from job_hunting_agent.config import load_billing_settings


def test_billing_starting_balance_defaults_to_zero_and_accepts_explicit_zero(tmp_path):
    """新账号默认不赠送余额，显式配置 0 也必须合法。"""

    default_settings = load_billing_settings(tmp_path / "missing.env", environ={})
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JOB_AGENT_BILLING_STARTING_BALANCE_YUAN=0\n",
        encoding="utf-8",
    )
    explicit_settings = load_billing_settings(env_file, environ={})

    assert default_settings.starting_balance_yuan == 0
    assert explicit_settings.starting_balance_yuan == 0


def test_billing_starting_balance_rejects_negative_values(tmp_path):
    """初始余额可以为 0，但不能为负数。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "JOB_AGENT_BILLING_STARTING_BALANCE_YUAN=-1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="不能小于 0"):
        load_billing_settings(env_file, environ={})
