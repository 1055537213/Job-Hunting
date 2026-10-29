"""Validate the Python repository to Java billing service contract.

This is intentionally a standalone CI check rather than a normal pytest test:
the Java process and Python process must share the same database schema.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_hunting_agent.config import PlatformBillingSettings  # noqa: E402
from job_hunting_agent.platform_billing import PlatformBillingClient, PlatformBillingError  # noqa: E402
from job_hunting_agent.sqlalchemy_store import SQLAlchemyStore  # noqa: E402


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required contract-test environment variable: {name}")
    return value


def main() -> None:
    database_url = required("JOB_AGENT_DATABASE_URL")
    base_url = required("JOB_AGENT_PLATFORM_CONTRACT_URL")
    internal_token = required("JOB_AGENT_JAVA_BILLING_INTERNAL_TOKEN")
    settings = PlatformBillingSettings(
        enabled=True,
        base_url=base_url,
        internal_token=internal_token,
        timeout_seconds=5,
    )
    client = PlatformBillingClient(settings)
    store = SQLAlchemyStore(database_url)
    suffix = uuid4().hex
    source_reference = f"contract-call-{suffix}"

    try:
        store.initialize()
        store.configure_platform_billing(client)
        account = store.create_account(
            email=f"python-java-contract-{suffix}@example.com",
            password_hash="contract-test-only-password-hash",
            display_name="Python Java contract test",
        )
        store.create_simulated_recharge_order(
            account.id,
            10,
            idempotency_key=f"contract-recharge-{suffix}",
            description="Python-Java 账务联调充值",
        )

        before = client.get_balance(account.id)
        assert before.balance_micro_yuan == 10_000_000, before

        unauthorized = PlatformBillingClient(
            PlatformBillingSettings(True, base_url, f"{internal_token}-wrong", 5)
        )
        try:
            unauthorized.get_balance(account.id, trace_id=f"contract-auth-{suffix}")
        except PlatformBillingError as error:
            assert error.code == "INTERNAL_UNAUTHORIZED", error
            assert error.status_code == 401, error
        else:
            raise AssertionError("Java billing accepted an invalid internal service token")
        print("Python -> Java billing authentication rejection: PASS")

        first = client.consume(
            account_id=account.id,
            amount_micro_yuan=2_000_000,
            token_count=80_000,
            source_reference=source_reference,
            description="Python-Java 账务联调扣费",
        )
        replay = client.consume(
            account_id=account.id,
            amount_micro_yuan=2_000_000,
            token_count=80_000,
            source_reference=source_reference,
            description="Python-Java 账务联调扣费重试",
        )
        try:
            client.consume(
                account_id=account.id,
                amount_micro_yuan=1_000_000,
                token_count=40_000,
                source_reference=source_reference,
                description="Python-Java 幂等键冲突",
            )
        except PlatformBillingError as error:
            assert error.code == "IDEMPOTENCY_CONFLICT", error
            assert error.status_code == 409, error
        else:
            raise AssertionError("Java billing accepted a conflicting idempotency replay")

        try:
            client.consume(
                account_id=account.id,
                amount_micro_yuan=9_000_000,
                token_count=360_000,
                source_reference=f"insufficient-{suffix}",
                description="Python-Java 余额不足",
            )
        except PlatformBillingError as error:
            assert error.code == "INSUFFICIENT_BALANCE", error
            assert error.status_code == 409, error
            assert str(error) == "余额不足，请先充值后重试", error
        else:
            raise AssertionError("Java billing accepted a charge larger than the balance")

        after = client.get_balance(account.id)
        local_projection = store.get_account_balance_summary(account.id)

        assert first.replayed is False, first
        assert replay.replayed is True, replay
        assert replay.ledger_entry_id == first.ledger_entry_id, (first, replay)
        assert after.balance_micro_yuan == 8_000_000, after
        assert local_projection.balance_micro_yuan == 8_000_000, local_projection
        assert local_projection.ledger_entry_count == 2, local_projection
        print("Python -> Java billing failure mapping and idempotency conflict: PASS")
        print(
            "Python-Java billing contract passed: "
            f"account={account.id}, ledger={first.ledger_entry_id}, "
            f"remote_balance={after.balance_micro_yuan}"
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
