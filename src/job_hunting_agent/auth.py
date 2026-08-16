"""账号认证与服务端 Session 工具。

本模块只处理“谁登录了”以及“登录状态是否仍然有效”，不负责候选人档案、
职位或 RAG 内容。业务资源的归属仍由结构化仓储和 Web 层校验。

设计要点：
- 密码使用 Argon2id 单向哈希，数据库永远不保存明文密码。
- 浏览器只保存随机 Session 令牌；数据库保存令牌哈希，便于主动撤销。
- Session 同时支持闲置过期和绝对过期，适合共享账号的本地 Web 工作区。
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

try:  # Argon2id 是首选；未安装可退回到标准库 scrypt，便于本地最小环境启动。
    from argon2 import PasswordHasher
    from argon2.exceptions import HashingError, VerificationError, VerifyMismatchError
except ImportError:  # pragma: no cover - CI 正常环境会安装 argon2-cffi
    PasswordHasher = None  # type: ignore[assignment,misc]
    HashingError = VerificationError = VerifyMismatchError = Exception  # type: ignore[assignment,misc]


# Argon2id 的参数由 PasswordHasher 使用安全默认值生成；参数会写在哈希字符串中。
PASSWORD_HASHER = PasswordHasher() if PasswordHasher is not None else None
PASSWORD_SCHEME = "argon2id" if PASSWORD_HASHER is not None else "scrypt"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass(frozen=True)
class AuthUser:
    """登录后供 Web/Agent 使用的最小账号信息。"""

    id: int
    email: str
    role: str
    status: str
    created_at: str
    last_login_at: str | None


@dataclass(frozen=True)
class AuthSession:
    """服务端 Session 记录；原始 token 只在登录响应中出现一次。"""

    id: int
    user_id: int
    created_at: str
    last_seen_at: str
    expires_at: str
    absolute_expires_at: str
    revoked_at: str | None

    @property
    def account_id(self) -> int:
        """兼容旧 `user_id` 命名，业务层统一使用 account_id。"""

        return self.user_id


def hash_password(password: str) -> str:
    """用 Argon2id 生成密码哈希；缺少可选依赖时使用标准库 scrypt。"""

    if len(password) < 8:
        raise ValueError("密码至少需要 8 个字符。")
    if PASSWORD_HASHER is not None:
        try:
            return PASSWORD_HASHER.hash(password)
        except HashingError as error:  # pragma: no cover - 由底层库报告环境问题
            raise ValueError("密码哈希失败，请检查 Argon2 运行环境。") from error
    # scrypt 同样是专门用于密码的内存困难 KDF，只作为 Argon2 不可用时的兼容退路。
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**15,
        r=8,
        p=1,
        dklen=32,
    )
    return "scrypt$1$" + salt.hex() + "$" + derived.hex()


def verify_password(password_hash: str, password: str) -> bool:
    """验证密码；任何哈希格式或密码不匹配都返回 False。"""

    if password_hash.startswith("$argon2") and PASSWORD_HASHER is not None:
        try:
            return PASSWORD_HASHER.verify(password_hash, password)
        except (VerificationError, VerifyMismatchError, ValueError):
            return False
    if not password_hash.startswith("scrypt$1$"):
        return False
    try:
        _, _, salt_hex, expected_hex = password_hash.split("$", 3)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=2**15,
            r=8,
            p=1,
            dklen=len(expected),
        )
    except (TypeError, ValueError):
        return False
    return secrets.compare_digest(actual, expected)


def new_session_token() -> str:
    """生成足够长的不可预测 Session 令牌。"""

    return secrets.token_urlsafe(48)


def session_token_hash(token: str) -> str:
    """只保存 Session 令牌的 SHA-256 哈希，避免数据库泄露后直接登录。"""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    """返回带时区的 UTC 时间，统一用于 Session 和用量记录。"""

    return datetime.now(UTC)


def iso_utc(value: datetime | None = None) -> str:
    """把 UTC 时间序列化为数据库可读的 ISO 字符串。"""

    return (value or utc_now()).isoformat(timespec="seconds")


def session_expiry(now: datetime | None = None) -> tuple[str, str]:
    """返回闲置 7 天和绝对 30 天两个过期时间。"""

    current = now or utc_now()
    idle_expiry = current + timedelta(days=7)
    absolute_expiry = current + timedelta(days=30)
    return iso_utc(idle_expiry), iso_utc(absolute_expiry)


def is_session_expired(expires_at: str, absolute_expires_at: str) -> bool:
    """判断 Session 是否达到任一过期边界。"""

    now = utc_now()
    try:
        return now >= datetime.fromisoformat(expires_at) or now >= datetime.fromisoformat(absolute_expires_at)
    except ValueError:
        # 数据损坏时按失效处理，避免把坏 Session 当成已登录状态。
        return True


class AuthError(Exception):
    """认证领域错误的基类，Web 层可统一转换为 401/403。"""


class AccountAlreadyExistsError(AuthError):
    """注册邮箱已经被占用。"""


class InvalidCredentialsError(AuthError):
    """邮箱或密码错误。"""


class AccountDisabledError(AuthError):
    """账号已被管理员禁用。"""


class SessionInvalidError(AuthError):
    """Cookie 对应的 Session 不存在、撤销或已过期。"""


class PermissionDeniedError(AuthError):
    """当前账号没有执行管理员操作的权限。"""


@dataclass(frozen=True)
class LoginResult:
    """登录结果；`session_token` 只用于写入 HttpOnly Cookie。"""

    account: object
    session_token: str
    session: object


class AuthService:
    """账号注册、登录和服务端 Session 的应用服务。

    该服务只依赖结构化仓储的认证方法，业务资源权限由 Web 层拿到当前账号后
    再调用 `account_id` 过滤查询。Session 默认闲置 7 天、绝对 30 天，测试时可注入
    `clock` 以稳定验证过期行为。
    """

    def __init__(
        self,
        store: object,
        idle_days: int = 7,
        max_days: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if idle_days <= 0 or max_days <= 0 or idle_days > max_days:
            raise ValueError("Session 过期天数必须为正，且闲置期限不能超过最长期限。")
        self.store = store
        self.idle_period = timedelta(days=idle_days)
        self.max_period = timedelta(days=max_days)
        self.clock = clock or utc_now

    @staticmethod
    def normalize_email(email: str) -> str:
        """清理并校验登录邮箱，统一小写避免同一邮箱重复注册。"""

        normalized = email.strip().lower()
        if not _EMAIL_RE.match(normalized):
            raise ValueError("请输入有效的邮箱地址。")
        return normalized

    def register(
        self,
        email: str,
        password: str,
        display_name: str | None = None,
    ):
        """开放注册普通账号；管理员账号通过 `create_admin` 单独创建。"""

        normalized = self.normalize_email(email)
        password_hash = hash_password(password)
        if self.store.get_account_by_email(normalized) is not None:
            raise AccountAlreadyExistsError("该邮箱已经注册。")
        try:
            return self.store.create_account(
                normalized,
                password_hash,
                display_name=(display_name or "").strip() or None,
                role="user",
            )
        except Exception as error:
            if "UNIQUE" in str(error).upper():
                raise AccountAlreadyExistsError("该邮箱已经注册。") from error
            raise

    def create_admin(
        self,
        email: str,
        password: str,
        display_name: str | None = None,
    ):
        """创建管理员账号；只应由首次引导或受保护的管理员流程调用。"""

        normalized = self.normalize_email(email)
        password_hash = hash_password(password)
        if self.store.get_account_by_email(normalized) is not None:
            raise AccountAlreadyExistsError("该邮箱已经注册。")
        try:
            return self.store.create_account(
                normalized,
                password_hash,
                display_name=(display_name or "").strip() or None,
                role="admin",
            )
        except Exception as error:
            if "UNIQUE" in str(error).upper():
                raise AccountAlreadyExistsError("该邮箱已经注册。") from error
            raise

    def authenticate(self, email: str, password: str):
        """校验账号密码，禁用账号不能建立新的 Session。"""

        normalized = self.normalize_email(email)
        result = self.store.get_account_by_email(normalized)
        if result is None or not verify_password(result[1], password):
            raise InvalidCredentialsError("邮箱或密码错误。")
        account = result[0]
        if account.status != "active":
            raise AccountDisabledError("账号已被禁用，请联系管理员。")
        return account

    def login(
        self,
        email: str,
        password: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> LoginResult:
        """登录并创建服务端 Session，返回原始 Cookie Token 一次。"""

        account = self.authenticate(email, password)
        now = self.clock()
        absolute = now + self.max_period
        idle = min(now + self.idle_period, absolute)
        token = new_session_token()
        session = self.store.save_auth_session(
            account.id,
            session_token_hash(token),
            iso_utc(now),
            iso_utc(now),
            iso_utc(idle),
            iso_utc(absolute),
            user_agent=user_agent,
            ip_address=ip_address,
        )
        return LoginResult(account=account, session_token=token, session=session)

    def current_account(self, token: str, touch: bool = True):
        """解析 Cookie 并返回当前账号；无效 Session 统一抛出 401 对应异常。"""

        if not token or len(token) < 20:
            raise SessionInvalidError("登录状态无效，请重新登录。")
        session = self.store.get_auth_session_by_token_hash(session_token_hash(token))
        if session is None or session.revoked_at is not None:
            raise SessionInvalidError("登录状态无效，请重新登录。")
        now = self.clock()
        try:
            idle_expiry = datetime.fromisoformat(session.expires_at)
            absolute_expiry = datetime.fromisoformat(session.absolute_expires_at)
        except ValueError as error:
            self.store.revoke_auth_session(session.id)
            raise SessionInvalidError("登录状态已损坏，请重新登录。") from error
        if now >= idle_expiry or now >= absolute_expiry:
            self.store.revoke_auth_session(session.id)
            raise SessionInvalidError("登录状态已过期，请重新登录。")
        account = self.store.get_account(session.account_id)
        if account.status != "active":
            self.store.revoke_auth_session(session.id)
            raise AccountDisabledError("账号已被禁用，请联系管理员。")
        if touch:
            next_idle = min(now + self.idle_period, absolute_expiry)
            self.store.touch_auth_session(session.id, iso_utc(now), iso_utc(next_idle))
        return account

    def logout(self, token: str) -> bool:
        """撤销当前设备 Session；重复退出也视为成功。"""

        session = self.store.get_auth_session_by_token_hash(session_token_hash(token))
        if session is None:
            return False
        self.store.revoke_auth_session(session.id)
        return True

    def logout_all(self, account_id: int) -> int:
        """撤销账号所有设备的登录状态。"""

        return self.store.revoke_all_auth_sessions(account_id)

    def set_account_status(self, account_id: int, status: str):
        """管理员变更账号状态；禁用时立即撤销全部 Session。"""

        if status not in {"active", "disabled"}:
            raise ValueError("账号状态只能是 active 或 disabled。")
        account = self.store.update_account_status(account_id, status)
        if status == "disabled":
            self.store.revoke_all_auth_sessions(account_id)
        return account

    def require_admin(self, account):
        """校验管理员角色，普通账号调用时抛出权限异常。"""

        if account.role != "admin" or account.status != "active":
            raise PermissionDeniedError("需要管理员权限。")
        return account
