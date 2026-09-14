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


def test_demo_recharge_defaults_are_environment_aware(tmp_path):
    """开发环境默认可演示充值，生产环境必须显式开启。"""

    development = load_billing_settings(tmp_path / "missing.env", environ={})
    production = load_billing_settings(
        tmp_path / "missing.env",
        environ={"JOB_AGENT_ENVIRONMENT": "production"},
    )
    enabled_production = load_billing_settings(
        tmp_path / "missing.env",
        environ={
            "JOB_AGENT_ENVIRONMENT": "production",
            "JOB_AGENT_DEMO_RECHARGE_ENABLED": "true",
            "JOB_AGENT_DEMO_RECHARGE_MAX_AMOUNT_YUAN": "20",
            "JOB_AGENT_DEMO_RECHARGE_MAX_TOTAL_YUAN": "50",
        },
    )

    assert development.demo_recharge_enabled is True
    assert production.demo_recharge_enabled is False
    assert enabled_production.demo_recharge_enabled is True
    assert enabled_production.demo_recharge_max_amount_yuan == 20
    assert enabled_production.demo_recharge_max_total_yuan == 50


def test_demo_recharge_total_limit_cannot_be_lower_than_single_limit(tmp_path):
    """演示累计额度不能小于单笔额度，避免前端展示无法兑现的金额。"""

    with pytest.raises(ValueError, match="不能小于"):
        load_billing_settings(
            tmp_path / "missing.env",
            environ={
                "JOB_AGENT_DEMO_RECHARGE_MAX_AMOUNT_YUAN": "20",
                "JOB_AGENT_DEMO_RECHARGE_MAX_TOTAL_YUAN": "10",
            },
        )
