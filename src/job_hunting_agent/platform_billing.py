"""Python 到 Java 平台账务服务的内部 HTTP 客户端。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx

from .config import PlatformBillingSettings


class PlatformBillingError(RuntimeError):
    """Java 账务服务返回的业务或基础设施错误。"""

    def __init__(self, code: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class PlatformBillingUnavailableError(PlatformBillingError):
    """Java 账务服务无法访问，调用方应稍后重试。"""

    def __init__(self, message: str = "平台账务服务暂时不可用，请稍后重试。") -> None:
        super().__init__("PLATFORM_BILLING_UNAVAILABLE", message)


@dataclass(frozen=True)
class PlatformBalanceProjection:
    account_id: int
    balance_micro_yuan: int
    total_recharge_micro_yuan: int
    total_consumed_micro_yuan: int
    ledger_entry_count: int


@dataclass(frozen=True)
class PlatformChargeResult:
    account_id: int
    balance_micro_yuan: int
    ledger_entry_id: int
    replayed: bool
    charged_micro_yuan: int


class PlatformBillingClient:
    """调用 Java 账务接口；每次请求都携带服务 Token 和 trace id。"""

    def __init__(self, settings: PlatformBillingSettings) -> None:
        if not settings.enabled:
            raise ValueError("Java 平台账务客户端只能在 enabled 配置下创建。")
        self.base_url = settings.base_url.rstrip("/")
        self.internal_token = settings.internal_token
        self.timeout_seconds = settings.timeout_seconds

    def get_balance(self, account_id: int, trace_id: str | None = None) -> PlatformBalanceProjection:
        payload = self._request(
            "GET",
            f"/internal/v1/billing/accounts/{int(account_id)}/balance",
            trace_id=trace_id,
        )
        try:
            return PlatformBalanceProjection(
                account_id=int(payload["account_id"]),
                balance_micro_yuan=int(payload["balance_micro_yuan"]),
                total_recharge_micro_yuan=int(payload["total_recharge_micro_yuan"]),
                total_consumed_micro_yuan=int(payload["total_consumed_micro_yuan"]),
                ledger_entry_count=int(payload["ledger_entry_count"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformBillingError(
                "INVALID_PLATFORM_RESPONSE",
                "平台账务服务返回了无效余额数据。",
            ) from error

    def consume(
        self,
        *,
        account_id: int,
        amount_micro_yuan: int,
        token_count: int,
        source_reference: str,
        description: str,
        trace_id: str | None = None,
    ) -> PlatformChargeResult:
        if amount_micro_yuan <= 0:
            raise ValueError("扣费金额必须大于 0。")
        payload = self._request(
            "POST",
            "/internal/v1/billing/consume",
            trace_id=trace_id,
            idempotency_key=source_reference,
            json={
                "account_id": int(account_id),
                "amount_micro_yuan": int(amount_micro_yuan),
                "token_count": max(0, int(token_count)),
                "source_reference": source_reference,
                "description": description,
            },
        )
        try:
            return PlatformChargeResult(
                account_id=int(payload["account_id"]),
                balance_micro_yuan=int(payload["balance_micro_yuan"]),
                ledger_entry_id=int(payload["ledger_entry_id"]),
                replayed=bool(payload["replayed"]),
                charged_micro_yuan=int(payload["charged_micro_yuan"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformBillingError(
                "INVALID_PLATFORM_RESPONSE",
                "平台账务服务返回了无效扣费数据。",
            ) from error

    def _request(
        self,
        method: str,
        path: str,
        *,
        trace_id: str | None,
        idempotency_key: str | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "X-Internal-Service-Token": self.internal_token,
            "X-Trace-Id": trace_id or f"platform-billing-{uuid4().hex}",
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=json,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as error:
            raise PlatformBillingUnavailableError() from error
        try:
            payload = response.json()
        except ValueError as error:
            raise PlatformBillingError(
                "INVALID_PLATFORM_RESPONSE",
                "平台账务服务返回了无法解析的响应。",
                response.status_code,
            ) from error
        if not isinstance(payload, dict):
            raise PlatformBillingError(
                "INVALID_PLATFORM_RESPONSE",
                "平台账务服务返回了无效响应。",
                response.status_code,
            )
        if response.status_code >= 500:
            raise PlatformBillingUnavailableError()
        if response.is_error:
            raise PlatformBillingError(
                str(payload.get("code") or "PLATFORM_BILLING_ERROR"),
                str(payload.get("message") or "平台账务服务请求失败。"),
                response.status_code,
            )
        return payload
