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
from job_hunting_agent.platform_billing import PlatformBillingClient  # noqa: E402
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
        after = client.get_balance(account.id)
        local_projection = store.get_account_balance_summary(account.id)

        assert first.replayed is False, first
        assert replay.replayed is True, replay
        assert replay.ledger_entry_id == first.ledger_entry_id, (first, replay)
        assert after.balance_micro_yuan == 8_000_000, after
        assert local_projection.balance_micro_yuan == 8_000_000, local_projection
        assert local_projection.ledger_entry_count == 2, local_projection
        print(
            "Python-Java billing contract passed: "
            f"account={account.id}, ledger={first.ledger_entry_id}, "
            f"remote_balance={after.balance_micro_yuan}"
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
