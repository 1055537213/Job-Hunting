package com.jobhunting.platform.auth;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Base64;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.UUID;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class AccountActionEmailService {
    private final JdbcTemplate jdbc;
    private final String secret;
    private final String baseUrl;
    private final int ttlMinutes, resetTtlMinutes, cooldownSeconds, accountLimit, sourceLimit;
    private final int maxAttempts, claimTimeout, retrySeconds, retentionDays;
    private final PasswordHasher passwords;

    public enum Purpose {
        VERIFY_EMAIL("verify_email"), RESET_PASSWORD("reset_password");
        final String value;
        Purpose(String value) { this.value = value; }
    }

    public AccountActionEmailService(JdbcTemplate jdbc, PasswordHasher passwords,
            @Value("${JOB_AGENT_ENVIRONMENT:development}") String environment,
            @Value("${platform.email.action-secret:development-only-account-action-secret}") String secret,
            @Value("${platform.email.public-base-url:http://localhost:8000}") String baseUrl,
            @Value("${platform.email.ttl-minutes:1440}") int ttlMinutes,
            @Value("${platform.email.reset-ttl-minutes:30}") int resetTtlMinutes,
            @Value("${platform.email.cooldown-seconds:60}") int cooldownSeconds,
            @Value("${platform.email.account-hourly-limit:5}") int accountLimit,
            @Value("${platform.email.source-hourly-limit:20}") int sourceLimit,
            @Value("${platform.email.max-attempts:5}") int maxAttempts,
            @Value("${platform.email.claim-timeout-seconds:300}") int claimTimeout,
            @Value("${platform.email.retry-base-seconds:30}") int retrySeconds,
            @Value("${platform.email.retention-days:30}") int retentionDays) {
        this.jdbc = jdbc;
        this.passwords = passwords;
        this.secret = secret;
        this.baseUrl = baseUrl.replaceAll("/+$", "");
        this.ttlMinutes = ttlMinutes;
        this.resetTtlMinutes = resetTtlMinutes;
        this.cooldownSeconds = cooldownSeconds;
        this.accountLimit = accountLimit;
        this.sourceLimit = sourceLimit;
        this.maxAttempts = maxAttempts;
        this.claimTimeout = claimTimeout;
        this.retrySeconds = retrySeconds;
        this.retentionDays = retentionDays;
        if (secret.isBlank() || List.of(ttlMinutes, resetTtlMinutes, cooldownSeconds, accountLimit, sourceLimit,
                maxAttempts, claimTimeout, retrySeconds, retentionDays).stream().anyMatch(v -> v <= 0)) {
            throw new IllegalArgumentException("Invalid platform email settings");
        }
        if ("production".equals(environment) && (secret.length() < 32
                || secret.equals("development-only-account-action-secret") || !baseUrl.startsWith("https://"))) {
            throw new IllegalArgumentException("Production verification requires a strong secret and HTTPS public URL");
        }
    }

    @Transactional
    public void request(Purpose purpose, String email, String source) {
        var accounts = jdbc.query("""
                SELECT id FROM accounts WHERE email = ? AND status = 'active'
                  AND deleted_at IS NULL AND (? = 'reset_password' OR email_verified_at IS NULL) FOR UPDATE
                """, (row, i) -> row.getLong("id"), email.strip().toLowerCase(Locale.ROOT), purpose.value);
        if (!accounts.isEmpty()) enqueue(purpose, accounts.getFirst(), source);
    }

    // Called inside registration's transaction as well as the resend transaction.
    public void enqueue(Purpose purpose, long accountId, String source) {
        String sourceHash = source == null || source.isBlank() ? null : hash(hmac("request-source:" + source));
        if (sourceHash != null) {
            jdbc.queryForObject("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", Object.class, sourceHash);
        }
        long recent = jdbc.queryForObject("""
                SELECT count(*) FROM platform_account_action_emails
                WHERE account_id = ? AND purpose = ? AND created_at > CURRENT_TIMESTAMP - ? * INTERVAL '1 second'
                """, Long.class, accountId, purpose.value, cooldownSeconds);
        long hourly = jdbc.queryForObject("""
                SELECT count(*) FROM platform_account_action_emails
                WHERE account_id = ? AND purpose = ? AND created_at > CURRENT_TIMESTAMP - INTERVAL '1 hour'
                """, Long.class, accountId, purpose.value);
        if (recent > 0 || hourly >= accountLimit) return;
        if (sourceHash != null && jdbc.queryForObject("""
                SELECT count(*) FROM platform_account_action_emails
                WHERE request_source_hash = ? AND purpose = ? AND created_at > CURRENT_TIMESTAMP - INTERVAL '1 hour'
                """, Long.class, sourceHash, purpose.value) >= sourceLimit) return;
        jdbc.update("""
                UPDATE platform_account_action_emails SET consumed_at = COALESCE(consumed_at, CURRENT_TIMESTAMP),
                  status = CASE WHEN status IN ('pending','sending','retrying') THEN 'cancelled' ELSE status END,
                  claim_key = NULL, claimed_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE account_id = ? AND purpose = ?
                """, accountId, purpose.value);
        String deliveryKey = UUID.randomUUID().toString();
        jdbc.update("""
                INSERT INTO platform_account_action_emails (account_id, recipient_email, delivery_key, purpose,
                  token_hash, credential_hash, request_source_hash, expires_at, status, attempt_count, max_attempts,
                  next_attempt_at, created_at, updated_at)
                SELECT id, email, ?, ?, ?,
                  CASE WHEN ? = 'reset_password' THEN encode(sha256(convert_to(password_hash, 'UTF8')), 'hex') ELSE NULL END,
                  ?, CURRENT_TIMESTAMP + ? * INTERVAL '1 minute',
                  'pending', 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                FROM accounts WHERE id = ? AND status = 'active' AND deleted_at IS NULL
                  AND (? = 'reset_password' OR email_verified_at IS NULL)
                """, deliveryKey, purpose.value, hash(rawToken(purpose, deliveryKey, accountId)), purpose.value, sourceHash,
                purpose == Purpose.RESET_PASSWORD ? resetTtlMinutes : ttlMinutes, maxAttempts, accountId, purpose.value);
    }

    @Transactional
    public long verify(String token) {
        long id = consume(Purpose.VERIFY_EMAIL, token);
        jdbc.update("UPDATE accounts SET email_verified_at = COALESCE(email_verified_at, CURRENT_TIMESTAMP), updated_at = CURRENT_TIMESTAMP WHERE id = ?", id);
        jdbc.update("""
                UPDATE platform_account_action_emails SET status = 'cancelled', claim_key = NULL,
                  claimed_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE account_id = ? AND purpose = 'verify_email' AND status IN ('pending','sending','retrying')
                """, id);
        return id;
    }

    @Transactional
    public long resetPassword(String token, String newPassword) {
        if (newPassword == null || newPassword.length() < 8 || newPassword.length() > 1024) {
            throw new AuthException("INVALID_REQUEST", "密码长度应为 8 至 1024 个字符。", HttpStatus.BAD_REQUEST);
        }
        long id = consume(Purpose.RESET_PASSWORD, token);
        jdbc.update("""
                UPDATE accounts SET password_hash = ?, must_change_password = FALSE,
                  email_verified_at = COALESCE(email_verified_at, CURRENT_TIMESTAMP), updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """, passwords.encode(newPassword), id);
        jdbc.update("UPDATE auth_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE account_id = ? AND revoked_at IS NULL", id);
        invalidateCredentialLinks(id);
        return id;
    }

    // Caller holds the account lock in the password-change/reset transaction.
    public void invalidateCredentialLinks(long id) {
        jdbc.update("""
                UPDATE platform_account_action_emails SET consumed_at = COALESCE(consumed_at, CURRENT_TIMESTAMP),
                  status = CASE WHEN status IN ('pending','sending','retrying') THEN 'cancelled' ELSE status END,
                  claim_key = NULL, claimed_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE account_id = ?
                """, id);
        // Retire legacy links too, so a previously issued Python link cannot restore an old credential.
        jdbc.update("UPDATE account_action_tokens SET consumed_at = COALESCE(consumed_at, CURRENT_TIMESTAMP) WHERE account_id = ?", id);
        jdbc.update("""
                UPDATE account_email_outbox SET status = 'cancelled', claimed_at = NULL,
                  updated_at = CURRENT_TIMESTAMP WHERE account_id = ? AND status IN ('pending','sending','retrying')
                """, id);
    }

    private long consume(Purpose purpose, String token) {
        var ids = jdbc.query("SELECT account_id FROM platform_account_action_emails WHERE token_hash = ? AND purpose = ?",
                (row, i) -> row.getLong(1), hash(token), purpose.value);
        if (ids.isEmpty()) throw invalidToken(purpose);
        long id = ids.getFirst();
        // Use the same account-first lock as Java session admission and password changes.
        var active = jdbc.query("SELECT password_hash FROM accounts WHERE id = ? AND status = 'active' AND deleted_at IS NULL FOR UPDATE",
                (row, i) -> row.getString(1), id);
        if (active.isEmpty()) throw invalidToken(purpose);
        int updated = jdbc.update("""
                UPDATE platform_account_action_emails SET consumed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE account_id = ? AND token_hash = ? AND purpose = ? AND consumed_at IS NULL AND expires_at > CURRENT_TIMESTAMP
                  AND status <> 'cancelled'
                  AND (? = 'verify_email' OR credential_hash = ?)
                """, id, hash(token), purpose.value, purpose.value,
                purpose == Purpose.RESET_PASSWORD ? hash(active.getFirst()) : null);
        if (updated != 1) throw invalidToken(purpose);
        return id;
    }

    @Transactional
    public List<Due> due(Purpose purpose) {
        jdbc.update("""
                UPDATE platform_account_action_emails v SET status = 'cancelled', claim_key = NULL,
                  claimed_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE status IN ('pending','sending','retrying') AND (consumed_at IS NOT NULL
                  OR expires_at <= CURRENT_TIMESTAMP OR NOT EXISTS (SELECT 1 FROM accounts a
                    WHERE a.id = v.account_id AND a.status = 'active' AND a.deleted_at IS NULL
                      AND ((v.purpose = 'reset_password' AND v.credential_hash = encode(sha256(convert_to(a.password_hash, 'UTF8')), 'hex'))
                        OR (v.purpose = 'verify_email' AND a.email_verified_at IS NULL))))
                """);
        jdbc.update("""
                UPDATE platform_account_action_emails SET status = 'failed', claim_key = NULL,
                  claimed_at = NULL, last_error_type = 'WorkerLostAfterFinalAttempt', updated_at = CURRENT_TIMESTAMP
                WHERE status = 'sending' AND attempt_count >= max_attempts
                  AND claimed_at < CURRENT_TIMESTAMP - ? * INTERVAL '1 second'
                """, claimTimeout);
        jdbc.update("""
                DELETE FROM platform_account_action_emails WHERE status IN ('sent','failed','cancelled')
                  AND (consumed_at IS NOT NULL OR expires_at <= CURRENT_TIMESTAMP)
                  AND updated_at < CURRENT_TIMESTAMP - ? * INTERVAL '1 day'
                """, retentionDays);
        return jdbc.query("""
                SELECT id, attempt_count FROM platform_account_action_emails WHERE purpose = ? AND attempt_count < max_attempts
                  AND ((status IN ('pending','retrying') AND next_attempt_at <= CURRENT_TIMESTAMP)
                    OR (status = 'sending' AND claimed_at < CURRENT_TIMESTAMP - ? * INTERVAL '1 second'))
                ORDER BY next_attempt_at, id LIMIT 100
                """, (row, i) -> new Due(row.getLong(1), row.getInt(2)), purpose.value, claimTimeout);
    }

    @Transactional
    public Claim claim(Purpose purpose, long id) {
        String key = UUID.randomUUID().toString();
        var claims = jdbc.query("""
                UPDATE platform_account_action_emails v SET status = 'sending', attempt_count = attempt_count + 1,
                  claim_key = ?, claimed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND purpose = ? AND consumed_at IS NULL AND expires_at > CURRENT_TIMESTAMP AND attempt_count < max_attempts
                  AND ((status IN ('pending','retrying') AND next_attempt_at <= CURRENT_TIMESTAMP)
                    OR (status = 'sending' AND claimed_at < CURRENT_TIMESTAMP - ? * INTERVAL '1 second'))
                  AND EXISTS (SELECT 1 FROM accounts a WHERE a.id = v.account_id AND a.status = 'active'
                    AND a.deleted_at IS NULL
                    AND ((v.purpose = 'reset_password' AND v.credential_hash = encode(sha256(convert_to(a.password_hash, 'UTF8')), 'hex'))
                      OR (v.purpose = 'verify_email' AND a.email_verified_at IS NULL)))
                RETURNING recipient_email, delivery_key, account_id, attempt_count
                """, (row, i) -> new Claim(id, key, row.getString(1),
                        baseUrl + "/login?" + purpose.value + "_token=" + rawToken(purpose, row.getString(2), row.getLong(3)), row.getInt(4)), key, id, purpose.value, claimTimeout);
        return claims.isEmpty() ? null : claims.getFirst();
    }

    @Transactional
    public boolean finish(Purpose purpose, long id, String key, boolean sent, String errorType) {
        return jdbc.update("""
                UPDATE platform_account_action_emails SET
                  status = CASE WHEN ? THEN 'sent' WHEN attempt_count >= max_attempts THEN 'failed' ELSE 'retrying' END,
                  sent_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                  next_attempt_at = CURRENT_TIMESTAMP + LEAST(3600, ? * POWER(2, LEAST(attempt_count - 1, 10))) * INTERVAL '1 second',
                  last_error_type = ?, claim_key = NULL, claimed_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND purpose = ? AND status = 'sending' AND claim_key = ?
                """, sent, sent, retrySeconds, sent ? null : errorType, id, purpose.value, key) == 1;
    }

    public Map<String, Object> observations(Purpose purpose) {
        Map<String, Long> summary = new LinkedHashMap<>();
        jdbc.query("SELECT status, count(*) FROM platform_account_action_emails WHERE purpose = ? GROUP BY status", row -> {
            summary.put(row.getString(1), row.getLong(2));
        }, purpose.value);
        var records = jdbc.query("""
                SELECT id, account_id, recipient_email, status, attempt_count, max_attempts,
                  last_error_type, created_at, updated_at FROM platform_account_action_emails WHERE purpose = ?
                ORDER BY created_at DESC, id DESC LIMIT 20
                """, (row, i) -> {
            Map<String, Object> record = new LinkedHashMap<>();
            for (String column : List.of("id", "account_id", "recipient_email", "status", "attempt_count",
                    "max_attempts", "last_error_type")) record.put(column, row.getObject(column));
            record.put("purpose", purpose.value);
            record.put("owner", "java");
            record.put("created_at", row.getTimestamp("created_at").toInstant().toString());
            record.put("updated_at", row.getTimestamp("updated_at").toInstant().toString());
            record.put("last_error_summary", row.getString("last_error_type") == null ? null : "邮件投递未完成。");
            return record;
        }, purpose.value);
        return Map.of("summary", summary, "records", records);
    }

    private String rawToken(Purpose purpose, String deliveryKey, long id) {
        return Base64.getUrlEncoder().withoutPadding().encodeToString(hmac("v2:" + purpose.value + ":" + id + ":" + deliveryKey));
    }

    private byte[] hmac(String value) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            return mac.doFinal(value.getBytes(StandardCharsets.UTF_8));
        } catch (java.security.GeneralSecurityException e) { throw new IllegalStateException("HMAC unavailable", e); }
    }

    private static String hash(String value) { return hash(value.getBytes(StandardCharsets.UTF_8)); }
    private static String hash(byte[] value) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value)); }
        catch (java.security.NoSuchAlgorithmException e) { throw new IllegalStateException("SHA-256 unavailable", e); }
    }
    private static AuthException invalidToken(Purpose purpose) {
        return purpose == Purpose.VERIFY_EMAIL
                ? new AuthException("INVALID_VERIFICATION_TOKEN", "验证链接无效或已过期。", HttpStatus.BAD_REQUEST)
                : new AuthException("INVALID_RESET_TOKEN", "重置链接无效或已过期。", HttpStatus.BAD_REQUEST);
    }
    public record Due(long id, int attempt_count) { }
    public record Claim(long id, String claim_key, String recipient_email, String action_url, int attempt_count) {
        @Override public String toString() { return "Claim[redacted]"; }
    }
}
