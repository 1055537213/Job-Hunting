"""Drain authenticated Python requests before account data is removed."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from threading import BoundedSemaphore, Lock

import anyio
import sqlalchemy as sa
from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)
LEASE_STATE_KEY = "account_work_lease"


class AccountWorkBusy(RuntimeError):
    """Requests are still draining or the guard has reached its connection budget."""


class AccountWorkDenied(RuntimeError):
    """The account was disabled after authentication but before admission."""


class AccountWorkLease:
    def __init__(self, connection, slots):
        self._connection = connection
        self._slots = slots
        self._lock = Lock()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.release()

    def release(self):
        with self._lock:
            connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            # NullPool closes the physical session, releasing all session locks.
            connection.close()
        except Exception as error:  # noqa: BLE001 - never hide a completed response.
            try:
                connection.invalidate()
            except Exception:  # noqa: BLE001 - close failure is logged without personal data.
                pass
            logger.warning("Account work connection close failed: %s", type(error).__name__)
        finally:
            self._slots.release()


class AccountWorkGuard:
    """Separate, bounded, autocommit connections; no transaction spans an LLM call."""

    def __init__(self, database_url: str, *, max_connections: int = 8):
        self._slots = BoundedSemaphore(max_connections)
        self._engine = sa.create_engine(
            database_url, poolclass=sa.pool.NullPool, isolation_level="AUTOCOMMIT",
            connect_args={"connect_timeout": 5},
        )

    def acquire(self, account_id: int, *, deletion: bool = False) -> AccountWorkLease:
        if not self._slots.acquire(blocking=False):
            raise AccountWorkBusy("账号请求保护暂时繁忙，请稍后重试。")
        connection = None
        try:
            connection = self._engine.connect()
            function = "pg_try_advisory_lock" if deletion else "pg_try_advisory_lock_shared"
            # Include the schema so isolated test databases do not contend with public.
            acquired = connection.execute(sa.text(
                f"SELECT {function}(hashtextextended(current_schema() || :key, 0))"
            ), {"key": f":account-work:{account_id}"}).scalar_one()
            if not acquired:
                raise AccountWorkBusy("账号仍有请求执行，注销清理将在稍后重试。")
            if not deletion:
                active = connection.execute(sa.text(
                    "SELECT status='active' AND deleted_at IS NULL FROM accounts WHERE id=:owner"
                ), {"owner": account_id}).scalar_one_or_none()
                if active is not True:
                    raise AccountWorkDenied("账号已被停用，请重新登录。")
            return AccountWorkLease(connection, self._slots)
        except BaseException:
            try:
                if connection is not None:
                    connection.close()
            finally:
                self._slots.release()
            raise

    @contextmanager
    def deletion(self, account_id: int):
        lease = self.acquire(account_id, deletion=True)
        try:
            yield
        finally:
            lease.release()

    def protect_request(self, request: Request, account_id: int):
        if LEASE_STATE_KEY in request.scope["state"]:
            return
        try:
            request.scope["state"][LEASE_STATE_KEY] = self.acquire(account_id)
        except AccountWorkDenied:
            raise HTTPException(403, "账号已被停用，请重新登录。") from None
        except Exception:  # noqa: BLE001 - fail closed without exposing connection details.
            raise HTTPException(503, "账号请求保护暂时不可用，请稍后重试。", headers={"Retry-After": "3"}) from None


class AccountWorkRoute(APIRoute):
    """Release only after the complete ASGI response, including stream/background work."""

    async def handle(self, scope, receive, send):
        try:
            await super().handle(scope, receive, send)
        finally:
            lease = scope.get("state", {}).pop(LEASE_STATE_KEY, None)
            if lease is not None:
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(lease.release)


class AccountWorkStreamingResponse(StreamingResponse):
    """Explicitly close the sync source before the route releases its account lease."""

    def __init__(self, content, **kwargs):
        self._source = content
        super().__init__(content, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            close = getattr(self._source, "close", None)
            if close is not None:
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(close)
