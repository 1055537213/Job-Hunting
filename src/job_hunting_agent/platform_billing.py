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


@dataclass(frozen=True)
class PlatformCreditResult:
    account_id: int
    balance_micro_yuan: int
    ledger_entry_id: int
    replayed: bool


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
        return self._balance_projection(payload)

    def personal_page(
        self,
        resource: str,
        *,
        account_id: int,
        session_token: str | None,
        limit: int,
        offset: int,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        if resource not in {"balance", "recharge/orders"}:
            raise ValueError("Unsupported personal billing resource")
        if not session_token:
            raise PlatformBillingError("SESSION_EXPIRED", "登录状态已过期，请重新登录。", 401)
        payload = self._request(
            "POST", f"/internal/v1/billing/me/{resource}", trace_id=trace_id,
            json={"session_token": session_token, "limit": limit, "offset": offset},
        )
        try:
            entries = payload["entries" if resource == "balance" else "orders"]
            if (
                not isinstance(entries, list)
                or len(entries) > limit
                or any(not isinstance(row, dict) or row.get("account_id") != account_id for row in entries)
                or type(payload["total"]) is not int
                or payload["total"] < 0
                or payload["limit"] != limit
                or payload["offset"] != offset
            ):
                raise ValueError("Invalid page or owner")
            if resource == "balance" and (
                payload["summary"]["account_id"] != account_id
                or not isinstance(payload["settings"], dict)
                or payload["page_size"] != 100
                or payload["max_pages"] is not None
            ):
                raise ValueError("Invalid balance projection")
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformBillingError("INVALID_PLATFORM_RESPONSE", "平台账务服务返回了无效分页数据。") from error
        return payload

    def admin_request(
        self, resource: str, *, session_token: str | None, account_id: int | None = None,
        order_id: int | None = None, actor_account_id: int | None = None, payload: dict[str, Any] | None = None,
        limit: int = 100, offset: int = 0, trace_id: str | None = None,
    ) -> dict[str, Any]:
        if not session_token:
            raise PlatformBillingError("SESSION_EXPIRED", "登录状态已过期，请重新登录。", 401)
        body: dict[str, Any] = {"session_token": session_token}
        if resource in {"ledger", "orders"}:
            path = f"/internal/v1/billing/admin/{resource}"
            body.update(account_id=account_id, limit=limit, offset=offset)
        elif resource == "events" and order_id is not None:
            path = f"/internal/v1/billing/admin/orders/{int(order_id)}/events"
        elif resource == "credit" and account_id is not None and payload is not None:
            path = f"/internal/v1/billing/admin/accounts/{int(account_id)}/credit"
            body["payload"] = payload
        else:
            raise ValueError("Unsupported administrator billing resource")
        result = self._request("POST", path, trace_id=trace_id, json=body)
        try:
            if resource in {"ledger", "orders"}:
                rows = result["entries" if resource == "ledger" else "orders"]
                if (not isinstance(rows, list) or len(rows) > limit
                        or any(not isinstance(row, dict) or account_id is not None and row["account_id"] != account_id for row in rows)
                        or type(result["total"]) is not int or result["total"] < 0
                        or result["limit"] != limit or result["offset"] != offset
                        or result["page_size"] != 100 or result["max_pages"] is not None):
                    raise ValueError("Invalid page")
            elif resource == "events":
                if (result["order"]["id"] != order_id or not isinstance(result["events"], list)
                        or any(row["recharge_order_id"] != order_id for row in result["events"])):
                    raise ValueError("Invalid order events")
            elif (result["entry"]["account_id"] != account_id or result["summary"]["account_id"] != account_id
                    or result["entry"]["operator_account_id"] != actor_account_id
                    or result["entry"]["entry_kind"] != "adjustment"):
                raise ValueError("Invalid credit identity")
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformBillingError("INVALID_PLATFORM_RESPONSE", "平台账务服务返回了无效管理员账务数据。") from error
        return result

    def recharge(
        self,
        *,
        account_id: int,
        amount_micro_yuan: int,
        source_reference: str,
        actor_account_id: int,
        description: str,
        max_amount_micro_yuan: int | None = None,
        max_total_micro_yuan: int | None = None,
        trace_id: str | None = None,
    ) -> PlatformBalanceProjection:
        if amount_micro_yuan <= 0:
            raise ValueError("充值金额必须大于 0。")
        payload = self._request(
            "POST",
            "/internal/v1/billing/recharge",
            trace_id=trace_id,
            idempotency_key=source_reference,
            json={
                "account_id": int(account_id),
                "amount_micro_yuan": int(amount_micro_yuan),
                "source_reference": source_reference,
                "actor_account_id": int(actor_account_id),
                "max_amount_micro_yuan": max_amount_micro_yuan,
                "max_total_micro_yuan": max_total_micro_yuan,
                "description": description,
            },
        )
        return self._balance_projection(payload)

    def personal_recharge(
        self, *, account_id: int, session_token: str | None, amount_yuan: str,
        note: str | None, idempotency_key: str | None, trace_id: str | None = None,
    ) -> dict[str, Any]:
        if not session_token:
            raise PlatformBillingError("SESSION_EXPIRED", "登录状态已过期，请重新登录。", 401)
        payload = self._request("POST", "/internal/v1/billing/me/recharge", trace_id=trace_id, json={
            "session_token": session_token,
            "payload": {"amount_yuan": amount_yuan, "note": note, "idempotency_key": idempotency_key},
        })
        try:
            order, entry, summary = payload["order"], payload["entry"], payload["summary"]
            if (any(not isinstance(row, dict) or row["account_id"] != account_id for row in (order, entry, summary))
                    or order["status"] != "paid" or order["payment_provider"] != "simulated"
                    or type(order["id"]) is not int or order["id"] <= 0
                    or entry["recharge_order_id"] != order["id"] or entry["entry_kind"] != "recharge"):
                raise ValueError("Invalid recharge identity")
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformBillingError("INVALID_PLATFORM_RESPONSE", "平台账务服务返回了无效充值数据。") from error
        return payload

    def _balance_projection(self, payload: dict[str, Any]) -> PlatformBalanceProjection:
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

    def credit(
        self,
        *,
        account_id: int,
        actor_account_id: int,
        amount_micro_yuan: int,
        idempotency_key: str,
        reason: str,
        session_token: str | None,
        trace_id: str | None = None,
    ) -> PlatformCreditResult:
        if not session_token:
            raise PlatformBillingError("SESSION_EXPIRED", "登录状态已过期，请重新登录。", 401)
        payload = self._request(
            "POST",
            "/internal/v1/billing/credit",
            trace_id=trace_id,
            idempotency_key=idempotency_key,
            json={
                "account_id": int(account_id),
                "actor_account_id": int(actor_account_id),
                "amount_micro_yuan": int(amount_micro_yuan),
                "idempotency_key": idempotency_key,
                "reason": reason,
                "session_token": session_token,
            },
        )
        try:
            result = PlatformCreditResult(
                account_id=int(payload["account_id"]),
                balance_micro_yuan=int(payload["balance_micro_yuan"]),
                ledger_entry_id=int(payload["ledger_entry_id"]),
                replayed=payload["replayed"],
            )
            if result.account_id != account_id or result.ledger_entry_id <= 0 or not isinstance(result.replayed, bool):
                raise ValueError("Invalid credit identity")
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformBillingError("INVALID_PLATFORM_RESPONSE", "平台账务服务返回了无效补款数据。") from error

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
        if response.status_code >= 500:
            raise PlatformBillingUnavailableError()
        try:
            payload = response.json()
        except ValueError as error:
            raise PlatformBillingError(
                "INVALID_PLATFORM_RESPONSE",
                "平台账务服务返回了无法解析的响应。",
                response.status_code if response.is_error else None,
            ) from error
        if not isinstance(payload, dict):
            raise PlatformBillingError(
                "INVALID_PLATFORM_RESPONSE",
                "平台账务服务返回了无效响应。",
                response.status_code if response.is_error else None,
            )
        if response.is_error:
            raise PlatformBillingError(
                str(payload.get("code") or "PLATFORM_BILLING_ERROR"),
                str(payload.get("message") or "平台账务服务请求失败。"),
                response.status_code,
            )
        return payload
