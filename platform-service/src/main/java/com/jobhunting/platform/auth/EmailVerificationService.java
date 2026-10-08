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
public class EmailVerificationService {
    private final JdbcTemplate jdbc;
    private final String secret;
    private final String baseUrl;
    private final int ttlMinutes, cooldownSeconds, accountLimit, sourceLimit;
    private final int maxAttempts, claimTimeout, retrySeconds, retentionDays;

    public EmailVerificationService(JdbcTemplate jdbc,
            @Value("${JOB_AGENT_ENVIRONMENT:development}") String environment,
            @Value("${platform.email.action-secret:development-only-account-action-secret}") String secret,
            @Value("${platform.email.public-base-url:http://localhost:8000}") String baseUrl,
            @Value("${platform.email.ttl-minutes:1440}") int ttlMinutes,
            @Value("${platform.email.cooldown-seconds:60}") int cooldownSeconds,
            @Value("${platform.email.account-hourly-limit:5}") int accountLimit,
            @Value("${platform.email.source-hourly-limit:20}") int sourceLimit,
            @Value("${platform.email.max-attempts:5}") int maxAttempts,
            @Value("${platform.email.claim-timeout-seconds:300}") int claimTimeout,
            @Value("${platform.email.retry-base-seconds:30}") int retrySeconds,
            @Value("${platform.email.retention-days:30}") int retentionDays) {
        this.jdbc = jdbc;
        this.secret = secret;
        this.baseUrl = baseUrl.replaceAll("/+$", "");
        this.ttlMinutes = ttlMinutes;
        this.cooldownSeconds = cooldownSeconds;
        this.accountLimit = accountLimit;
        this.sourceLimit = sourceLimit;
        this.maxAttempts = maxAttempts;
        this.claimTimeout = claimTimeout;
        this.retrySeconds = retrySeconds;
        this.retentionDays = retentionDays;
        if (secret.isBlank() || List.of(ttlMinutes, cooldownSeconds, accountLimit, sourceLimit,
                maxAttempts, claimTimeout, retrySeconds, retentionDays).stream().anyMatch(v -> v <= 0)) {
            throw new IllegalArgumentException("Invalid platform email settings");
        }
        if ("production".equals(environment) && (secret.length() < 32
                || secret.equals("development-only-account-action-secret") || !baseUrl.startsWith("https://"))) {
            throw new IllegalArgumentException("Production verification requires a strong secret and HTTPS public URL");
        }
    }

    @Transactional
    public void request(String email, String source) {
        var accounts = jdbc.query("""
                SELECT id FROM accounts WHERE email = ? AND status = 'active'
                  AND deleted_at IS NULL AND email_verified_at IS NULL FOR UPDATE
                """, (row, i) -> row.getLong("id"), email.strip().toLowerCase(Locale.ROOT));
        if (!accounts.isEmpty()) enqueue(accounts.getFirst(), source);
    }

    // Called inside registration's transaction as well as the resend transaction.
    public void enqueue(long accountId, String source) {
        String sourceHash = source == null || source.isBlank() ? null : hash(hmac("request-source:" + source));
        if (sourceHash != null) {
            jdbc.queryForObject("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", Object.class, sourceHash);
        }
        long recent = jdbc.queryForObject("""
                SELECT count(*) FROM platform_email_verifications
                WHERE account_id = ? AND created_at > CURRENT_TIMESTAMP - ? * INTERVAL '1 second'
                """, Long.class, accountId, cooldownSeconds);
        long hourly = jdbc.queryForObject("""
                SELECT count(*) FROM platform_email_verifications
                WHERE account_id = ? AND created_at > CURRENT_TIMESTAMP - INTERVAL '1 hour'
                """, Long.class, accountId);
        if (recent > 0 || hourly >= accountLimit) return;
        if (sourceHash != null && jdbc.queryForObject("""
                SELECT count(*) FROM platform_email_verifications
                WHERE request_source_hash = ? AND created_at > CURRENT_TIMESTAMP - INTERVAL '1 hour'
                """, Long.class, sourceHash) >= sourceLimit) return;
        jdbc.update("""
                UPDATE platform_email_verifications SET consumed_at = COALESCE(consumed_at, CURRENT_TIMESTAMP),
                  status = CASE WHEN status IN ('pending','sending','retrying') THEN 'cancelled' ELSE status END,
                  claim_key = NULL, claimed_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE account_id = ?
                """, accountId);
        String deliveryKey = UUID.randomUUID().toString();
        jdbc.update("""
                INSERT INTO platform_email_verifications (account_id, recipient_email, delivery_key,
                  token_hash, request_source_hash, expires_at, status, attempt_count, max_attempts,
                  next_attempt_at, created_at, updated_at)
                SELECT id, email, ?, ?, ?, CURRENT_TIMESTAMP + ? * INTERVAL '1 minute',
                  'pending', 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                FROM accounts WHERE id = ? AND status = 'active' AND deleted_at IS NULL
                  AND email_verified_at IS NULL
                """, deliveryKey, hash(rawToken(deliveryKey, accountId)), sourceHash, ttlMinutes, maxAttempts, accountId);
    }

    @Transactional
    public long verify(String token) {
        var ids = jdbc.query("SELECT account_id FROM platform_email_verifications WHERE token_hash = ?",
                (row, i) -> row.getLong(1), hash(token));
        if (ids.isEmpty()) throw invalidToken();
        long id = ids.getFirst();
        var active = jdbc.query("SELECT id FROM accounts WHERE id = ? AND status = 'active' AND deleted_at IS NULL FOR UPDATE",
                (row, i) -> row.getLong(1), id);
        if (active.isEmpty()) throw invalidToken();
        int updated = jdbc.update("""
                UPDATE platform_email_verifications SET consumed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE account_id = ? AND token_hash = ? AND consumed_at IS NULL AND expires_at > CURRENT_TIMESTAMP
                """, id, hash(token));
        if (updated != 1) throw invalidToken();
        jdbc.update("UPDATE accounts SET email_verified_at = COALESCE(email_verified_at, CURRENT_TIMESTAMP), updated_at = CURRENT_TIMESTAMP WHERE id = ?", id);
        jdbc.update("""
                UPDATE platform_email_verifications SET status = 'cancelled', claim_key = NULL,
                  claimed_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE account_id = ? AND status IN ('pending','sending','retrying')
                """, id);
        return id;
    }

    @Transactional
    public List<Due> due() {
        jdbc.update("""
                UPDATE platform_email_verifications v SET status = 'cancelled', claim_key = NULL,
                  claimed_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE status IN ('pending','sending','retrying') AND (consumed_at IS NOT NULL
                  OR expires_at <= CURRENT_TIMESTAMP OR NOT EXISTS (SELECT 1 FROM accounts a
                    WHERE a.id = v.account_id AND a.status = 'active' AND a.deleted_at IS NULL AND a.email_verified_at IS NULL))
                """);
        jdbc.update("""
                UPDATE platform_email_verifications SET status = 'failed', claim_key = NULL,
                  claimed_at = NULL, last_error_type = 'WorkerLostAfterFinalAttempt', updated_at = CURRENT_TIMESTAMP
                WHERE status = 'sending' AND attempt_count >= max_attempts
                  AND claimed_at < CURRENT_TIMESTAMP - ? * INTERVAL '1 second'
                """, claimTimeout);
        jdbc.update("""
                DELETE FROM platform_email_verifications WHERE status IN ('sent','failed','cancelled')
                  AND (consumed_at IS NOT NULL OR expires_at <= CURRENT_TIMESTAMP)
                  AND updated_at < CURRENT_TIMESTAMP - ? * INTERVAL '1 day'
                """, retentionDays);
        return jdbc.query("""
                SELECT id, attempt_count FROM platform_email_verifications WHERE attempt_count < max_attempts
                  AND ((status IN ('pending','retrying') AND next_attempt_at <= CURRENT_TIMESTAMP)
                    OR (status = 'sending' AND claimed_at < CURRENT_TIMESTAMP - ? * INTERVAL '1 second'))
                ORDER BY next_attempt_at, id LIMIT 100
                """, (row, i) -> new Due(row.getLong(1), row.getInt(2)), claimTimeout);
    }

    @Transactional
    public Claim claim(long id) {
        String key = UUID.randomUUID().toString();
        var claims = jdbc.query("""
                UPDATE platform_email_verifications v SET status = 'sending', attempt_count = attempt_count + 1,
                  claim_key = ?, claimed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND consumed_at IS NULL AND expires_at > CURRENT_TIMESTAMP AND attempt_count < max_attempts
                  AND ((status IN ('pending','retrying') AND next_attempt_at <= CURRENT_TIMESTAMP)
                    OR (status = 'sending' AND claimed_at < CURRENT_TIMESTAMP - ? * INTERVAL '1 second'))
                  AND EXISTS (SELECT 1 FROM accounts a WHERE a.id = v.account_id AND a.status = 'active'
                    AND a.deleted_at IS NULL AND a.email_verified_at IS NULL)
                RETURNING recipient_email, delivery_key, account_id, attempt_count
                """, (row, i) -> new Claim(id, key, row.getString(1),
                        baseUrl + "/login?verify_email_token=" + rawToken(row.getString(2), row.getLong(3)), row.getInt(4)), key, id, claimTimeout);
        return claims.isEmpty() ? null : claims.getFirst();
    }

    @Transactional
    public boolean finish(long id, String key, boolean sent, String errorType) {
        return jdbc.update("""
                UPDATE platform_email_verifications SET
                  status = CASE WHEN ? THEN 'sent' WHEN attempt_count >= max_attempts THEN 'failed' ELSE 'retrying' END,
                  sent_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                  next_attempt_at = CURRENT_TIMESTAMP + LEAST(3600, ? * POWER(2, LEAST(attempt_count - 1, 10))) * INTERVAL '1 second',
                  last_error_type = ?, claim_key = NULL, claimed_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'sending' AND claim_key = ?
                """, sent, sent, retrySeconds, sent ? null : errorType, id, key) == 1;
    }

    public Map<String, Object> observations() {
        Map<String, Long> summary = new LinkedHashMap<>();
        jdbc.query("SELECT status, count(*) FROM platform_email_verifications GROUP BY status", row -> {
            summary.put(row.getString(1), row.getLong(2));
        });
        var records = jdbc.query("""
                SELECT id, account_id, recipient_email, status, attempt_count, max_attempts,
                  last_error_type, created_at, updated_at FROM platform_email_verifications
                ORDER BY created_at DESC, id DESC LIMIT 20
                """, (row, i) -> {
            Map<String, Object> record = new LinkedHashMap<>();
            for (String column : List.of("id", "account_id", "recipient_email", "status", "attempt_count",
                    "max_attempts", "last_error_type")) record.put(column, row.getObject(column));
            record.put("purpose", "verify_email");
            record.put("owner", "java");
            record.put("created_at", row.getTimestamp("created_at").toInstant().toString());
            record.put("updated_at", row.getTimestamp("updated_at").toInstant().toString());
            record.put("last_error_summary", row.getString("last_error_type") == null ? null : "邮件投递未完成。");
            return record;
        });
        return Map.of("summary", summary, "records", records);
    }

    private String rawToken(String deliveryKey, long id) {
        return Base64.getUrlEncoder().withoutPadding().encodeToString(hmac("v2:verify_email:" + id + ":" + deliveryKey));
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
    private static AuthException invalidToken() {
        return new AuthException("INVALID_VERIFICATION_TOKEN", "验证链接无效或已过期。", HttpStatus.BAD_REQUEST);
    }
    public record Due(long id, int attempt_count) { }
    public record Claim(long id, String claim_key, String recipient_email, String action_url, int attempt_count) {
        @Override public String toString() { return "Claim[redacted]"; }
    }
}
