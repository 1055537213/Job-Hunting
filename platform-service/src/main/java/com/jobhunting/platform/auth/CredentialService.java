package com.jobhunting.platform.auth;

import java.util.Locale;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

@Service
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class CredentialService implements CredentialVerifier {
    private final JdbcTemplate jdbc;
    private final PasswordVerifier passwords;
    private final String dummyHash;

    public CredentialService(JdbcTemplate jdbc, PasswordVerifier passwords) {
        this.jdbc = jdbc;
        this.passwords = passwords;
        // Missing accounts still execute a password KDF before returning the same error.
        this.dummyHash = org.springframework.security.crypto.argon2.Argon2PasswordEncoder
                .defaultsForSpringSecurity_v5_8().encode(java.util.UUID.randomUUID().toString());
    }

    @Override
    public long verify(String email, String password, boolean verificationRequired) {
        var rows = jdbc.query(
                "SELECT id, password_hash, status, email_verified_at, deleted_at FROM accounts WHERE email = ?",
                (result, index) -> new Credentials(
                        result.getLong("id"), result.getString("password_hash"), result.getString("status"),
                        result.getTimestamp("email_verified_at") != null, result.getTimestamp("deleted_at") != null),
                email.strip().toLowerCase(Locale.ROOT));
        Credentials account = rows.isEmpty() ? null : rows.getFirst();
        boolean matched = passwords.matches(password, account == null ? dummyHash : account.hash());
        if (account == null || !matched || account.deleted()) {
            throw new AuthException("INVALID_CREDENTIALS", "邮箱或密码错误。", HttpStatus.UNAUTHORIZED);
        }
        if (!"active".equals(account.status())) {
            throw new AuthException("ACCOUNT_DISABLED", "账号已被禁用。", HttpStatus.FORBIDDEN);
        }
        if (verificationRequired && !account.verified()) {
            throw new AuthException("EMAIL_UNVERIFIED", "请先完成邮箱验证。", HttpStatus.FORBIDDEN);
        }
        return account.id();
    }

    private record Credentials(long id, String hash, String status, boolean verified, boolean deleted) { }
}
