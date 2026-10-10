package com.jobhunting.platform.auth;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.HexFormat;
import java.util.Locale;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class SessionService {
    private final JdbcTemplate jdbc;
    private final CredentialService credentials;
    private final PasswordVerifier verifier;
    private final PasswordHasher hasher;
    private final AccountActionEmailService emails;
    private final SecureRandom random = new SecureRandom();

    public SessionService(JdbcTemplate jdbc, CredentialService credentials, PasswordVerifier verifier,
            PasswordHasher hasher, AccountActionEmailService emails) {
        this.jdbc = jdbc;
        this.credentials = credentials;
        this.verifier = verifier;
        this.hasher = hasher;
        this.emails = emails;
    }

    @Transactional
    public Login login(String email, String password, boolean verificationRequired, String userAgent, String ip) {
        // Account-first locks serialize login with reset/change/delete. Verify inside this transaction.
        jdbc.query("SELECT id FROM accounts WHERE email = ? FOR UPDATE", (row, i) -> row.getLong(1),
                email.strip().toLowerCase(Locale.ROOT));
        long id = credentials.verify(email, password, verificationRequired);
        byte[] bytes = new byte[48];
        random.nextBytes(bytes);
        String token = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
        jdbc.update("""
                INSERT INTO auth_sessions (account_id, token_hash, created_at, last_seen_at, expires_at,
                  absolute_expires_at, user_agent, ip_address)
                VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '7 days',
                  CURRENT_TIMESTAMP + INTERVAL '30 days', ?, ?)
                """, id, digest(token), userAgent, ip);
        jdbc.update("UPDATE accounts SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", id);
        return new Login(id, token);
    }

    @Transactional
    public Long resolve(String token) {
        Long id = lockAccount(token);
        if (id == null) return null;
        var ids = jdbc.query("""
                UPDATE auth_sessions SET last_seen_at = CURRENT_TIMESTAMP,
                  expires_at = LEAST(absolute_expires_at, CURRENT_TIMESTAMP + INTERVAL '7 days')
                WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > CURRENT_TIMESTAMP
                  AND absolute_expires_at > CURRENT_TIMESTAMP RETURNING account_id
                """, (row, i) -> row.getLong(1), digest(token));
        return ids.isEmpty() ? null : id;
    }

    @Transactional
    public void logout(String token) {
        // An expired/disabled token can still be revoked; logout remains idempotent.
        var ids = jdbc.query("SELECT account_id FROM auth_sessions WHERE token_hash = ?",
                (row, i) -> row.getLong(1), digest(token));
        if (ids.isEmpty()) return;
        jdbc.query("SELECT id FROM accounts WHERE id = ? FOR UPDATE", (row, i) -> row.getLong(1), ids.getFirst());
        jdbc.update("UPDATE auth_sessions SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP) WHERE token_hash = ?", digest(token));
    }

    @Transactional
    public int logoutAll(String token, String requestId) {
        long id = requireSession(token);
        int count = revokeAll(id);
        String role = jdbc.queryForObject("SELECT role FROM accounts WHERE id = ?", String.class, id);
        if ("admin".equals(role)) {
            jdbc.update("""
                    INSERT INTO admin_audit_events (actor_account_id, target_account_id, action, target_type,
                      target_id, outcome, summary, details_json, request_id, created_at)
                    VALUES (?, ?, 'auth.logout_all_devices', 'account', ?, 'succeeded', ?, CAST(? AS jsonb), ?, CURRENT_TIMESTAMP)
                    """, id, id, Long.toString(id), "撤销账号 #" + id + " 的全部登录会话。",
                    "{\"revoked_sessions\":" + count + "}", requestId);
        }
        return count;
    }

    @Transactional
    public void changePassword(String token, String currentPassword, String newPassword) {
        if (newPassword == null || newPassword.length() < 8 || newPassword.length() > 1024) {
            throw new AuthException("INVALID_REQUEST", "密码长度应为 8 至 1024 个字符。", HttpStatus.BAD_REQUEST);
        }
        long id = requireSession(token);
        String hash = jdbc.queryForObject("SELECT password_hash FROM accounts WHERE id = ?", String.class, id);
        if (!verifier.matches(currentPassword, hash)) {
            throw new AuthException("INVALID_CURRENT_PASSWORD", "当前密码错误。", HttpStatus.BAD_REQUEST);
        }
        if (verifier.matches(newPassword, hash)) {
            throw new AuthException("PASSWORD_UNCHANGED", "新密码不能与当前密码相同。", HttpStatus.BAD_REQUEST);
        }
        jdbc.update("UPDATE accounts SET password_hash = ?, must_change_password = FALSE, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                hasher.encode(newPassword), id);
        revokeAll(id);
        emails.invalidateCredentialLinks(id);
    }

    @Transactional
    long prepareDeletion(String token, String currentPassword) {
        // Validate the Java-owned session and password before changing status.
        long id = requireSession(token);
        String hash = jdbc.queryForObject("SELECT password_hash FROM accounts WHERE id = ?", String.class, id);
        if (!verifier.matches(currentPassword, hash)) {
            throw new AuthException("INVALID_CURRENT_PASSWORD", "当前密码错误。", HttpStatus.BAD_REQUEST);
        }
        String role = jdbc.queryForObject("SELECT role FROM accounts WHERE id = ?", String.class, id);
        if ("admin".equals(role)) {
            throw new AuthException("ADMIN_DELETE_FORBIDDEN", "管理员账号不能通过个人中心自助注销。", HttpStatus.FORBIDDEN);
        }
        jdbc.update("UPDATE accounts SET status = 'disabled', updated_at = CURRENT_TIMESTAMP WHERE id = ?", id);
        revokeAll(id);
        emails.invalidateCredentialLinks(id);
        return id;
    }

    private int revokeAll(long id) {
        return jdbc.update("UPDATE auth_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE account_id = ? AND revoked_at IS NULL", id);
    }

    // Package-private so other Java-owned account operations can reuse the exact
    // session lock and expiry checks instead of trusting an account id from Python.
    long requireSession(String token) {
        Long id = lockAccount(token);
        if (id == null) {
            throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
        }
        return id;
    }

    private Long lockAccount(String token) {
        var rows = jdbc.query("""
                SELECT a.id, a.status, a.deleted_at FROM accounts a
                JOIN auth_sessions s ON s.account_id = a.id WHERE s.token_hash = ? FOR UPDATE OF a
                """, (row, i) -> new Account(row.getLong(1), row.getString(2), row.getTimestamp(3) != null), digest(token));
        if (rows.isEmpty() || rows.getFirst().deleted()) return null;
        // Check session eligibility before exposing disabled account status.
        int valid = jdbc.queryForObject("""
                SELECT count(*) FROM auth_sessions WHERE token_hash = ? AND revoked_at IS NULL
                  AND expires_at > CURRENT_TIMESTAMP AND absolute_expires_at > CURRENT_TIMESTAMP
                """, Integer.class, digest(token));
        if (valid == 0) return null;
        if (!"active".equals(rows.getFirst().status())) {
            throw new AuthException("ACCOUNT_DISABLED", "账号已被禁用。", HttpStatus.FORBIDDEN);
        }
        return rows.getFirst().id();
    }

    static String digest(String token) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(token.getBytes(StandardCharsets.UTF_8))); }
        catch (java.security.NoSuchAlgorithmException e) { throw new IllegalStateException("SHA-256 unavailable", e); }
    }
    private record Account(long id, String status, boolean deleted) { }
    public record Login(long account_id, String session_token) {
        @Override public String toString() { return "Login[redacted]"; }
    }
}
