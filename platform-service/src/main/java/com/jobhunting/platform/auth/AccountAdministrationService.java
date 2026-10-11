package com.jobhunting.platform.auth;

import java.sql.Timestamp;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import tools.jackson.core.JacksonException;
import tools.jackson.databind.ObjectMapper;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * Java owner for administrator account listing, status changes and bootstrap.
 *
 * The policy advisory lock is deliberately acquired before any account lock so
 * concurrent administrator operations cannot remove the last active admin or
 * deadlock in different actor/target lock orders.
 */
@Service
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class AccountAdministrationService {
    private static final String POLICY_LOCK = "platform-admin-policy-v1";

    private final JdbcTemplate jdbc;
    private final ObjectMapper objectMapper;
    private final SessionService sessions;
    private final RegistrationService registrations;
    private final AccountActionEmailService emails;
    private final PasswordHasher passwordHasher;

    public AccountAdministrationService(
            JdbcTemplate jdbc,
            ObjectMapper objectMapper,
            SessionService sessions,
            RegistrationService registrations,
            AccountActionEmailService emails,
            PasswordHasher passwordHasher) {
        this.jdbc = jdbc;
        this.objectMapper = objectMapper;
        this.sessions = sessions;
        this.registrations = registrations;
        this.emails = emails;
        this.passwordHasher = passwordHasher;
    }

    @Transactional
    public AccountList list(String sessionToken) {
        lockPolicy();
        requireAdmin(sessionToken);
        List<AccountView> accounts = jdbc.query("""
                SELECT id, email, display_name, role, status, must_change_password,
                       email_verified_at, deleted_at, created_at, updated_at
                FROM accounts WHERE deleted_at IS NULL ORDER BY id
                """, (row, index) -> account(row));
        return new AccountList(accounts);
    }

    @Transactional
    public AccountResponse current(String sessionToken) {
        // Resolution locks the account and renews the idle window in this transaction.
        Long accountId = sessions.resolve(sessionToken);
        if (accountId == null) {
            throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
        }
        return new AccountResponse(accountById(accountId));
    }

    @Transactional
    public AccountResponse updateProfile(String sessionToken, String displayName, String requestId) {
        Long accountId = sessions.resolve(sessionToken);
        if (accountId == null) {
            throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
        }
        AccountView current = lockAccount(accountId);
        if (current == null) {
            throw new AuthException("ACCOUNT_NOT_FOUND", "账号不存在。", HttpStatus.NOT_FOUND);
        }
        String nextDisplayName = displayName == null ? null : displayName.strip();
        if (nextDisplayName != null && nextDisplayName.isBlank()) nextDisplayName = null;
        if (!java.util.Objects.equals(current.display_name(), nextDisplayName)) {
            jdbc.update("UPDATE accounts SET display_name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    nextDisplayName, accountId);
            String details = json(Map.of(
                    "previous_display_name_present", current.display_name() != null,
                    "next_display_name_present", nextDisplayName != null));
            jdbc.update("""
                    INSERT INTO admin_audit_events (
                        actor_account_id, target_account_id, action, target_type, target_id,
                        outcome, summary, details_json, request_id, created_at
                    ) VALUES (?, ?, 'account.profile_updated', 'account', ?, 'succeeded', ?,
                              CAST(? AS jsonb), ?, CURRENT_TIMESTAMP)
                    """, accountId, accountId, Long.toString(accountId),
                    "账号 #" + accountId + " 更新了显示名称。", details, cap(requestId));
        }
        return new AccountResponse(accountById(accountId));
    }

    @Transactional
    public AccountResponse updateStatus(
            String sessionToken, long targetId, String nextStatus, String requestId) {
        if (!"active".equals(nextStatus) && !"disabled".equals(nextStatus)) {
            throw new AuthException("INVALID_REQUEST", "状态只能是 active 或 disabled。", HttpStatus.BAD_REQUEST);
        }
        lockPolicy();
        long actorId = requireAdmin(sessionToken);
        AccountView target = lockAccount(targetId);
        if (target == null) {
            throw new AuthException("ACCOUNT_NOT_FOUND", "账号不存在。", HttpStatus.NOT_FOUND);
        }
        if ("disabled".equals(nextStatus) && target.account_id() == actorId) {
            throw new AuthException("SELF_DISABLE_FORBIDDEN", "不能禁用当前正在使用的管理员账号。", HttpStatus.BAD_REQUEST);
        }
        if ("disabled".equals(nextStatus)
                && "admin".equals(target.role())
                && "active".equals(target.status())
                && activeAdminCount() <= 1) {
            throw new AuthException(
                    "LAST_ADMIN_FORBIDDEN", "至少需要保留一个可用管理员账号。", HttpStatus.BAD_REQUEST);
        }

        jdbc.update("UPDATE accounts SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                nextStatus, targetId);
        if ("disabled".equals(nextStatus)) {
            jdbc.update("UPDATE auth_sessions SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP) "
                    + "WHERE account_id = ? AND revoked_at IS NULL", targetId);
            emails.invalidateCredentialLinks(targetId);
        }
        String details = json(Map.of(
                "previous_status", target.status(),
                "next_status", nextStatus,
                "target_role", target.role()));
        jdbc.update("""
                INSERT INTO admin_audit_events (
                    actor_account_id, target_account_id, action, target_type, target_id,
                    outcome, summary, details_json, request_id, created_at
                ) VALUES (?, ?, 'account.status_updated', 'account', ?, 'succeeded', ?,
                          CAST(? AS jsonb), ?, CURRENT_TIMESTAMP)
                """, actorId, targetId, Long.toString(targetId),
                "账号 #" + targetId + " 状态从 " + target.status() + " 更新为 " + nextStatus + "。",
                details, cap(requestId));
        return new AccountResponse(accountById(targetId));
    }

    @Transactional
    public BootstrapResult bootstrap(String email, String password, String displayName) {
        lockPolicy();
        Long admins = jdbc.queryForObject(
                "SELECT count(*) FROM accounts WHERE role = 'admin' AND deleted_at IS NULL",
                Long.class);
        if (admins != null && admins > 0) {
            return new BootstrapResult(false, null);
        }

        long accountId = registrations.register(
                email, password, displayName, true, List.of());
        jdbc.update("""
                UPDATE accounts SET role = 'admin', must_change_password = TRUE,
                    email_verified_at = COALESCE(email_verified_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """, accountId);
        return new BootstrapResult(true, accountId);
    }

    @Transactional
    public AccountResponse prepareDeletion(String sessionToken, String currentPassword, String requestId) {
        long accountId = sessions.prepareDeletion(sessionToken, currentPassword);
        jdbc.update("""
                INSERT INTO admin_audit_events (actor_account_id, target_account_id, action, target_type,
                  target_id, outcome, summary, details_json, request_id, created_at)
                VALUES (?, ?, 'account.deletion_requested', 'account', ?, 'succeeded', ?, CAST(? AS jsonb), ?, CURRENT_TIMESTAMP)
                """, accountId, accountId, Long.toString(accountId),
                "账号 #" + accountId + " 已通过注销校验并进入异步清理。",
                json(Map.of("source", "self_service")), cap(requestId));
        return new AccountResponse(accountById(accountId));
    }

    @Transactional
    public DeletionResult completeDeletion(long accountId, String taskKey, String requestId) {
        var rows = jdbc.query(
                "SELECT id, status, deleted_at FROM accounts WHERE id = ? FOR UPDATE",
                (row, index) -> new DeletionAccount(row.getString("status"), row.getTimestamp("deleted_at") != null),
                accountId);
        if (rows.isEmpty()) {
            throw new AuthException("ACCOUNT_NOT_FOUND", "账号不存在。", HttpStatus.NOT_FOUND);
        }
        DeletionAccount account = rows.getFirst();
        if (account.deleted()) return new DeletionResult(accountId, true);
        if (!"disabled".equals(account.status())) {
            throw new AuthException("DELETION_NOT_ADMITTED", "账号尚未通过注销准入。", HttpStatus.CONFLICT);
        }
        String deletedAt = UUID.randomUUID().toString();
        jdbc.update("""
                UPDATE accounts SET email = ?, password_hash = ?, display_name = NULL,
                    status = 'disabled', must_change_password = FALSE,
                    email_verified_at = NULL, deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND deleted_at IS NULL
                """, "deleted-" + accountId + "-" + deletedAt + "@invalid.local",
                passwordHasher.encode("deleted-" + UUID.randomUUID()), accountId);
        emails.invalidateCredentialLinks(accountId);
        jdbc.update("""
                INSERT INTO admin_audit_events (actor_account_id, target_account_id, action, target_type,
                  target_id, outcome, summary, details_json, request_id, created_at)
                VALUES (?, ?, 'account.deletion_completed', 'account', ?, 'succeeded', ?, CAST(? AS jsonb), ?, CURRENT_TIMESTAMP)
                """, accountId, accountId, Long.toString(accountId),
                "账号 #" + accountId + " 已完成注销清理。",
                json(Map.of("task_key", cap(taskKey))), cap(requestId));
        return new DeletionResult(accountId, true);
    }

    private long requireAdmin(String sessionToken) {
        long actorId = sessions.requireSession(sessionToken);
        String role = jdbc.queryForObject("SELECT role FROM accounts WHERE id = ?", String.class, actorId);
        if (!"admin".equals(role)) {
            throw new AuthException("ADMIN_REQUIRED", "需要管理员权限。", HttpStatus.FORBIDDEN);
        }
        return actorId;
    }

    private void lockPolicy() {
        jdbc.queryForObject(
                "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
                Object.class, POLICY_LOCK);
    }

    private long activeAdminCount() {
        Long count = jdbc.queryForObject("""
                SELECT count(*) FROM accounts
                WHERE role = 'admin' AND status = 'active' AND deleted_at IS NULL
                """, Long.class);
        return count == null ? 0 : count;
    }

    private AccountView lockAccount(long id) {
        var rows = jdbc.query("""
                SELECT id, email, display_name, role, status, must_change_password,
                       email_verified_at, deleted_at, created_at, updated_at
                FROM accounts WHERE id = ? AND deleted_at IS NULL FOR UPDATE
                """, (row, index) -> account(row), id);
        return rows.isEmpty() ? null : rows.getFirst();
    }

    private AccountView accountById(long id) {
        return jdbc.queryForObject("""
                SELECT id, email, display_name, role, status, must_change_password,
                       email_verified_at, deleted_at, created_at, updated_at
                FROM accounts WHERE id = ? AND deleted_at IS NULL
                """, (row, index) -> account(row), id);
    }

    private static AccountView account(java.sql.ResultSet row) throws java.sql.SQLException {
        return new AccountView(
                row.getLong("id"),
                row.getString("email"),
                row.getString("display_name"),
                row.getString("role"),
                row.getString("status"),
                row.getBoolean("must_change_password"),
                instant(row.getTimestamp("email_verified_at")),
                instant(row.getTimestamp("deleted_at")),
                instant(row.getTimestamp("created_at")),
                instant(row.getTimestamp("updated_at")));
    }

    private static String instant(Timestamp value) {
        return value == null ? null : value.toInstant().toString();
    }

    private String json(Map<String, Object> value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (JacksonException exception) {
            throw new IllegalStateException("Unable to serialize admin audit details", exception);
        }
    }

    private static String cap(String value) {
        if (value == null || value.isBlank()) return null;
        return value.substring(0, Math.min(128, value.length()));
    }

    public record AccountList(List<AccountView> accounts) { }
    public record AccountResponse(AccountView account) { }
    public record BootstrapResult(boolean created, Long account_id) { }
    public record DeletionResult(long account_id, boolean deleted) { }
    private record DeletionAccount(String status, boolean deleted) { }
    public record AccountView(
            long account_id,
            String email,
            String display_name,
            String role,
            String status,
            boolean must_change_password,
            String email_verified_at,
            String deleted_at,
            String created_at,
            String updated_at) { }
}
