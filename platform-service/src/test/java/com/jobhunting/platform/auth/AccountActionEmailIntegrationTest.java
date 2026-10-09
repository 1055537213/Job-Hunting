package com.jobhunting.platform.auth;

import static org.assertj.core.api.Assertions.*;
import java.util.concurrent.*;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfSystemProperty;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.NONE, properties = {
    "platform.auth.enabled=true", "platform.email.cooldown-seconds=1",
    "platform.email.source-hourly-limit=1", "platform.email.claim-timeout-seconds=1",
    "platform.email.max-attempts=2", "platform.email.retention-days=1"
})
@Testcontainers
@EnabledIfSystemProperty(named = "run.integration.tests", matches = "true")
class AccountActionEmailIntegrationTest {
    @Container static final PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:16-alpine");
    @DynamicPropertySource static void datasource(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", postgres::getJdbcUrl);
        registry.add("spring.datasource.username", postgres::getUsername);
        registry.add("spring.datasource.password", postgres::getPassword);
    }
    @Autowired AccountActionEmailService service;
    @Autowired JdbcTemplate jdbc;
    @Autowired PasswordVerifier passwords;
    @Autowired PasswordHasher hasher;
    @Autowired SessionService sessions;

    @BeforeEach void reset() {
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS accounts (id integer PRIMARY KEY, email varchar(254) UNIQUE,
              password_hash text, must_change_password boolean DEFAULT TRUE,
              role varchar(32) DEFAULT 'user',
              status varchar(32), deleted_at timestamptz, email_verified_at timestamptz, updated_at timestamptz)
            """);
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS platform_account_action_emails (
              id serial PRIMARY KEY, purpose varchar(32) DEFAULT 'verify_email', account_id integer REFERENCES accounts(id), recipient_email varchar(254),
              delivery_key varchar(64) UNIQUE, token_hash varchar(64) UNIQUE, credential_hash varchar(64), request_source_hash varchar(64),
              expires_at timestamptz NOT NULL, consumed_at timestamptz, status varchar(32),
              attempt_count integer, max_attempts integer, next_attempt_at timestamptz,
              claimed_at timestamptz, claim_key varchar(64), sent_at timestamptz, last_error_type varchar(128),
              created_at timestamptz, updated_at timestamptz)
            """);
        jdbc.execute("TRUNCATE platform_account_action_emails, accounts RESTART IDENTITY CASCADE");
        jdbc.execute("""
                CREATE TABLE IF NOT EXISTS auth_sessions (id serial PRIMARY KEY, account_id integer REFERENCES accounts(id),
                  token_hash varchar(64) UNIQUE, created_at timestamptz, last_seen_at timestamptz,
                  expires_at timestamptz, absolute_expires_at timestamptz, revoked_at timestamptz, user_agent text, ip_address varchar(64))
                """);
        jdbc.execute("""
                CREATE TABLE IF NOT EXISTS admin_audit_events (id serial PRIMARY KEY, actor_account_id integer,
                  target_account_id integer, action varchar(96), target_type varchar(64), target_id varchar(160),
                  outcome varchar(32), summary text, details_json jsonb, request_id varchar(128), created_at timestamptz)
                """);
        jdbc.execute("TRUNCATE admin_audit_events");
        jdbc.execute("CREATE TABLE IF NOT EXISTS account_action_tokens (id serial PRIMARY KEY, account_id integer REFERENCES accounts(id), consumed_at timestamptz)");
        jdbc.execute("CREATE TABLE IF NOT EXISTS account_email_outbox (id serial PRIMARY KEY, account_id integer REFERENCES accounts(id), status varchar(32), claimed_at timestamptz, updated_at timestamptz)");
        jdbc.update("INSERT INTO accounts (id,email,status) VALUES (1,'a@example.com','active'),(2,'b@example.com','active')");
        jdbc.update("UPDATE accounts SET password_hash='old-hash'");
    }
    private String token(AccountActionEmailService.Claim claim) {
        return claim.action_url().split("verify_email_token=")[1];
    }

    private SessionService.Login login() {
        jdbc.update("UPDATE accounts SET password_hash=?, email_verified_at=CURRENT_TIMESTAMP WHERE id=1", hasher.encode("password-123"));
        return sessions.login("A@example.com", "password-123", true, "test-agent", "127.0.0.1");
    }

    @Test void sessionsAreHashedAndLogoutIsDeviceScopedAndIdempotent() {
        var first = login();
        var second = sessions.login("a@example.com", "password-123", true, null, null);
        assertThat(first.toString()).doesNotContain(first.session_token());
        assertThat(jdbc.queryForObject("SELECT count(*) FROM auth_sessions WHERE token_hash=?", Integer.class,
                SessionService.digest(first.session_token()))).isEqualTo(1);
        assertThat(sessions.resolve(first.session_token())).isEqualTo(1L);
        sessions.logout(first.session_token());
        sessions.logout(first.session_token());
        sessions.logout("missing");
        assertThat(sessions.resolve(first.session_token())).isNull();
        assertThat(sessions.resolve(second.session_token())).isEqualTo(1L);
        assertThat(sessions.resolve("missing")).isNull();
    }

    @Test void invalidCredentialsAndRequiredVerificationNeverIssueSessions() {
        jdbc.update("UPDATE accounts SET password_hash=? WHERE id=1", hasher.encode("password-123"));
        assertThatThrownBy(() -> sessions.login("missing@example.com", "password-123", false, null, null)).isInstanceOf(AuthException.class);
        assertThatThrownBy(() -> sessions.login("a@example.com", "wrong", false, null, null)).isInstanceOf(AuthException.class);
        assertThatThrownBy(() -> sessions.login("a@example.com", "password-123", true, null, null)).isInstanceOf(AuthException.class);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM auth_sessions", Integer.class)).isZero();
    }

    @Test void auditFailureRollsBackLogoutAll() {
        var loggedIn = login();
        jdbc.update("UPDATE accounts SET role='admin' WHERE id=1");
        jdbc.execute("ALTER TABLE admin_audit_events ADD CONSTRAINT reject_audit CHECK (action <> 'auth.logout_all_devices')");
        try {
            assertThatThrownBy(() -> sessions.logoutAll(loggedIn.session_token(), "trace"))
                .isInstanceOf(org.springframework.dao.DataIntegrityViolationException.class);
            assertThat(sessions.resolve(loggedIn.session_token())).isEqualTo(1L);
        } finally { jdbc.execute("ALTER TABLE admin_audit_events DROP CONSTRAINT reject_audit"); }
    }

    @Test void sessionsHonorBothExpiryLimitsAndAccountStatus() {
        var first = login();
        jdbc.update("UPDATE auth_sessions SET absolute_expires_at=CURRENT_TIMESTAMP+INTERVAL '1 hour'");
        assertThat(sessions.resolve(first.session_token())).isEqualTo(1L);
        assertThat(jdbc.queryForObject("SELECT expires_at=absolute_expires_at FROM auth_sessions", Boolean.class)).isTrue();
        jdbc.update("UPDATE auth_sessions SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second'");
        assertThat(sessions.resolve(first.session_token())).isNull();
        var second = sessions.login("a@example.com", "password-123", true, null, null);
        jdbc.update("UPDATE accounts SET status='disabled' WHERE id=1");
        assertThatThrownBy(() -> sessions.resolve(second.session_token())).isInstanceOf(AuthException.class);
        sessions.logout(second.session_token());
        assertThat(sessions.resolve(second.session_token())).isNull();
        jdbc.update("UPDATE accounts SET status='active',deleted_at=CURRENT_TIMESTAMP WHERE id=1");
        assertThatThrownBy(() -> sessions.login("a@example.com", "password-123", false, null, null)).isInstanceOf(AuthException.class);
    }

    @Test void logoutAllIncludesCurrentDeviceAndPreservesOtherAccountsAndAuditsAdmins() {
        var first = login();
        sessions.login("a@example.com", "password-123", true, null, null);
        jdbc.update("UPDATE accounts SET role='admin' WHERE id=1");
        jdbc.update("INSERT INTO auth_sessions (account_id) VALUES (2)");
        assertThat(sessions.logoutAll(first.session_token(), "trace-logout")).isEqualTo(2);
        assertThat(sessions.resolve(first.session_token())).isNull();
        assertThatThrownBy(() -> sessions.logoutAll(first.session_token(), "trace")).isInstanceOf(AuthException.class);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM auth_sessions WHERE account_id=2 AND revoked_at IS NULL", Integer.class)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT details_json->>'revoked_sessions' FROM admin_audit_events", String.class)).isEqualTo("2");
    }

    @Test void changingPasswordInvalidatesAllDevicesAndAllCredentialLinks() {
        var first = login();
        var second = sessions.login("a@example.com", "password-123", true, null, null);
        var reset = resetClaim("a@example.com");
        assertThatThrownBy(() -> sessions.changePassword(first.session_token(), "wrong", "new-password-456")).isInstanceOf(AuthException.class);
        assertThatThrownBy(() -> sessions.changePassword(first.session_token(), "password-123", "password-123")).isInstanceOf(AuthException.class);
        assertThat(sessions.resolve(first.session_token())).isEqualTo(1L);
        sessions.changePassword(first.session_token(), "password-123", "new-password-456");
        assertThat(sessions.resolve(first.session_token())).isNull();
        assertThat(sessions.resolve(second.session_token())).isNull();
        assertThatThrownBy(() -> service.resetPassword(resetToken(reset), "another-password")).isInstanceOf(AuthException.class);
        assertThatThrownBy(() -> sessions.login("a@example.com", "password-123", false, null, null)).isInstanceOf(AuthException.class);
        assertThat(sessions.login("a@example.com", "new-password-456", true, null, null).account_id()).isEqualTo(1);
    }

    @Test void loginRacingResetNeverLeavesALiveOldCredentialSession() throws Exception {
        var old = login();
        var claim = resetClaim("a@example.com");
        try (ExecutorService pool = Executors.newFixedThreadPool(2)) {
            CountDownLatch start = new CountDownLatch(1);
            Future<SessionService.Login> loggingIn = pool.submit(() -> { start.await();
                try { return sessions.login("a@example.com", "password-123", false, null, null); }
                catch (AuthException expected) { return null; } });
            Future<Long> resetting = pool.submit(() -> { start.await(); return service.resetPassword(resetToken(claim), "new-password-456"); });
            start.countDown();
            resetting.get(10, TimeUnit.SECONDS);
            var raced = loggingIn.get(10, TimeUnit.SECONDS);
            assertThat(sessions.resolve(old.session_token())).isNull();
            if (raced != null) assertThat(sessions.resolve(raced.session_token())).isNull();
        }
        assertThat(jdbc.queryForObject("SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL", Integer.class)).isZero();
    }

    @Test void passwordChangeRollbackRetainsCredentialSessionAndResetToken() {
        var loggedIn = login();
        var claim = resetClaim("a@example.com");
        jdbc.execute("ALTER TABLE auth_sessions ADD CONSTRAINT reject_revoke CHECK (revoked_at IS NULL)");
        try {
            assertThatThrownBy(() -> sessions.changePassword(loggedIn.session_token(), "password-123", "new-password-456"))
                .isInstanceOf(org.springframework.dao.DataIntegrityViolationException.class);
            assertThat(sessions.resolve(loggedIn.session_token())).isEqualTo(1L);
            assertThat(passwords.matches("password-123", jdbc.queryForObject("SELECT password_hash FROM accounts WHERE id=1", String.class))).isTrue();
            assertThat(jdbc.queryForObject("SELECT consumed_at IS NULL FROM platform_account_action_emails WHERE id=?", Boolean.class, claim.id())).isTrue();
        } finally { jdbc.execute("ALTER TABLE auth_sessions DROP CONSTRAINT reject_revoke"); }
    }
    @Test void verifiesExactlyOnceAndStoresOnlyDigest() throws Exception {
        service.request(AccountActionEmailService.Purpose.VERIFY_EMAIL, "A@example.com", null);
        var claim = service.claim(AccountActionEmailService.Purpose.VERIFY_EMAIL, service.due(AccountActionEmailService.Purpose.VERIFY_EMAIL).getFirst().id());
        String raw = token(claim);
        assertThat(jdbc.queryForObject("SELECT token_hash FROM platform_account_action_emails", String.class)).hasSize(64).isNotEqualTo(raw);
        assertThat(service.claim(AccountActionEmailService.Purpose.VERIFY_EMAIL, claim.id())).isNull();
        assertThat(service.finish(AccountActionEmailService.Purpose.VERIFY_EMAIL, claim.id(), claim.claim_key(), true, null)).isTrue();
        var observations = service.observations(AccountActionEmailService.Purpose.VERIFY_EMAIL);
        assertThat(observations.toString()).doesNotContain(raw, claim.claim_key(), "token_hash", "delivery_key", "action_url");
        try (ExecutorService pool = Executors.newFixedThreadPool(2)) {
            CountDownLatch start = new CountDownLatch(1);
            Callable<Boolean> verify = () -> { start.await(); try { service.verify(raw); return true; }
                catch (AuthException expected) { return false; } };
            Future<Boolean> first = pool.submit(verify), second = pool.submit(verify);
            start.countDown();
            assertThat(first.get(10, TimeUnit.SECONDS) ^ second.get(10, TimeUnit.SECONDS)).isTrue();
        }
        assertThat(jdbc.queryForObject("SELECT email_verified_at IS NOT NULL FROM accounts WHERE id=1", Boolean.class)).isTrue();
        assertThatThrownBy(() -> service.verify("invalid")).isInstanceOf(AuthException.class);
    }
    @Test void expiredOrDisabledAccountsCannotVerify() {
        service.request(AccountActionEmailService.Purpose.VERIFY_EMAIL, "a@example.com", null);
        var claim = service.claim(AccountActionEmailService.Purpose.VERIFY_EMAIL, service.due(AccountActionEmailService.Purpose.VERIFY_EMAIL).getFirst().id());
        jdbc.update("UPDATE platform_account_action_emails SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 minute'");
        assertThatThrownBy(() -> service.verify(token(claim))).isInstanceOf(AuthException.class);
        assertThat(service.due(AccountActionEmailService.Purpose.VERIFY_EMAIL)).isEmpty();
        service.request(AccountActionEmailService.Purpose.VERIFY_EMAIL, "b@example.com", null);
        var other = service.claim(AccountActionEmailService.Purpose.VERIFY_EMAIL, service.due(AccountActionEmailService.Purpose.VERIFY_EMAIL).getFirst().id());
        jdbc.update("UPDATE accounts SET status='disabled' WHERE id=2");
        assertThatThrownBy(() -> service.verify(token(other))).isInstanceOf(AuthException.class);
        assertThat(service.finish(AccountActionEmailService.Purpose.VERIFY_EMAIL, claim.id(), claim.claim_key(), true, null)).isFalse();
    }
    @Test void staleWorkersCannotOverwriteNewClaimAndFinalAttemptStops() {
        service.request(AccountActionEmailService.Purpose.VERIFY_EMAIL, "a@example.com", null);
        long id = service.due(AccountActionEmailService.Purpose.VERIFY_EMAIL).getFirst().id();
        var first = service.claim(AccountActionEmailService.Purpose.VERIFY_EMAIL, id);
        jdbc.update("UPDATE platform_account_action_emails SET claimed_at=CURRENT_TIMESTAMP-INTERVAL '10 seconds'");
        var second = service.claim(AccountActionEmailService.Purpose.VERIFY_EMAIL, id);
        assertThat(service.finish(AccountActionEmailService.Purpose.VERIFY_EMAIL, id, first.claim_key(), true, null)).isFalse();
        assertThat(service.finish(AccountActionEmailService.Purpose.VERIFY_EMAIL, id, second.claim_key(), false, "SmtpDeliveryFailed")).isTrue();
        assertThat(service.due(AccountActionEmailService.Purpose.VERIFY_EMAIL)).isEmpty();
        assertThat(jdbc.queryForObject("SELECT status FROM platform_account_action_emails", String.class)).isEqualTo("failed");
    }
    @Test void resendLimitsHideAccountExistenceAndPreserveLiveSentTokens() {
        service.request(AccountActionEmailService.Purpose.VERIFY_EMAIL, "missing@example.com", "source");
        service.request(AccountActionEmailService.Purpose.VERIFY_EMAIL, "a@example.com", "source");
        service.request(AccountActionEmailService.Purpose.VERIFY_EMAIL, "a@example.com", "source");
        service.request(AccountActionEmailService.Purpose.VERIFY_EMAIL, "b@example.com", "source");
        assertThat(service.due(AccountActionEmailService.Purpose.VERIFY_EMAIL)).hasSize(1);
        var claim = service.claim(AccountActionEmailService.Purpose.VERIFY_EMAIL, service.due(AccountActionEmailService.Purpose.VERIFY_EMAIL).getFirst().id());
        service.finish(AccountActionEmailService.Purpose.VERIFY_EMAIL, claim.id(), claim.claim_key(), true, null);
        jdbc.update("UPDATE platform_account_action_emails SET updated_at=CURRENT_TIMESTAMP-INTERVAL '2 days'");
        service.due(AccountActionEmailService.Purpose.VERIFY_EMAIL);
        assertThat(service.verify(token(claim))).isEqualTo(1L);
    }

    private AccountActionEmailService.Claim resetClaim(String email) {
        service.request(AccountActionEmailService.Purpose.RESET_PASSWORD, email, null);
        return service.claim(AccountActionEmailService.Purpose.RESET_PASSWORD,
                service.due(AccountActionEmailService.Purpose.RESET_PASSWORD).getFirst().id());
    }

    private String resetToken(AccountActionEmailService.Claim claim) {
        return claim.action_url().split("reset_password_token=")[1];
    }

    @Test void resetsExactlyOnceRevokesSessionsAndRetiresEveryOldLink() throws Exception {
        jdbc.update("UPDATE accounts SET email_verified_at=CURRENT_TIMESTAMP WHERE id=1");
        jdbc.update("INSERT INTO auth_sessions (account_id) VALUES (1),(1),(2)");
        jdbc.update("INSERT INTO account_action_tokens (account_id) VALUES (1),(2)");
        jdbc.update("INSERT INTO account_email_outbox (account_id,status) VALUES (1,'pending'),(2,'pending')");
        var claim = resetClaim("a@example.com");
        String raw = resetToken(claim);
        assertThatThrownBy(() -> service.verify(raw)).isInstanceOf(AuthException.class);
        assertThat(service.claim(AccountActionEmailService.Purpose.VERIFY_EMAIL, claim.id())).isNull();
        assertThat(service.finish(AccountActionEmailService.Purpose.VERIFY_EMAIL, claim.id(), claim.claim_key(), true, null)).isFalse();
        assertThat(service.observations(AccountActionEmailService.Purpose.RESET_PASSWORD).toString())
            .doesNotContain(raw, claim.claim_key(), "token_hash", "delivery_key", "action_url");
        try (ExecutorService pool = Executors.newFixedThreadPool(2)) {
            CountDownLatch start = new CountDownLatch(1);
            Callable<Boolean> reset = () -> { start.await(); try { service.resetPassword(raw, "new-password-123"); return true; }
                catch (AuthException expected) { return false; } };
            Future<Boolean> first = pool.submit(reset), second = pool.submit(reset);
            start.countDown();
            assertThat(first.get(10, TimeUnit.SECONDS) ^ second.get(10, TimeUnit.SECONDS)).isTrue();
        }
        String encoded = jdbc.queryForObject("SELECT password_hash FROM accounts WHERE id=1", String.class);
        assertThat(encoded).startsWith("$argon2id$").doesNotContain("new-password-123");
        assertThat(passwords.matches("new-password-123", encoded)).isTrue();
        assertThat(passwords.matches("old-password-123", encoded)).isFalse();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM auth_sessions WHERE account_id=1 AND revoked_at IS NULL", Long.class)).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM auth_sessions WHERE account_id=2 AND revoked_at IS NULL", Long.class)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT consumed_at IS NOT NULL FROM account_action_tokens WHERE account_id=1", Boolean.class)).isTrue();
        assertThat(jdbc.queryForObject("SELECT status FROM account_email_outbox WHERE account_id=1", String.class)).isEqualTo("cancelled");
        assertThat(jdbc.queryForObject("SELECT must_change_password FROM accounts WHERE id=1", Boolean.class)).isFalse();
        assertThat(service.finish(AccountActionEmailService.Purpose.RESET_PASSWORD, claim.id(), claim.claim_key(), true, null)).isFalse();
    }

    @Test void verificationCannotBeUsedForResetAndWeakPasswordsDoNotConsumeToken() {
        service.request(AccountActionEmailService.Purpose.VERIFY_EMAIL, "a@example.com", null);
        var verification = service.claim(AccountActionEmailService.Purpose.VERIFY_EMAIL,
                service.due(AccountActionEmailService.Purpose.VERIFY_EMAIL).getFirst().id());
        assertThatThrownBy(() -> service.resetPassword(token(verification), "new-password-123")).isInstanceOf(AuthException.class);
        var reset = resetClaim("a@example.com");
        assertThatThrownBy(() -> service.resetPassword(resetToken(reset), "short")).isInstanceOf(AuthException.class);
        assertThat(service.resetPassword(resetToken(reset), "new-password-123")).isEqualTo(1);
        assertThatThrownBy(() -> service.verify(token(verification))).isInstanceOf(AuthException.class);
        assertThat(jdbc.queryForObject("SELECT email_verified_at IS NOT NULL FROM accounts WHERE id=1", Boolean.class)).isTrue();
    }

    @Test void resetExpiryDisabledAccountAndRetryFencingAreEnforced() {
        var first = resetClaim("a@example.com");
        jdbc.update("UPDATE platform_account_action_emails SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 minute' WHERE id=?", first.id());
        assertThatThrownBy(() -> service.resetPassword(resetToken(first), "new-password-123")).isInstanceOf(AuthException.class);
        var other = resetClaim("b@example.com");
        jdbc.update("UPDATE platform_account_action_emails SET claimed_at=CURRENT_TIMESTAMP-INTERVAL '10 seconds' WHERE id=?", other.id());
        var retry = service.claim(AccountActionEmailService.Purpose.RESET_PASSWORD, other.id());
        assertThat(service.finish(AccountActionEmailService.Purpose.RESET_PASSWORD, other.id(), other.claim_key(), true, null)).isFalse();
        assertThat(service.finish(AccountActionEmailService.Purpose.RESET_PASSWORD, retry.id(), retry.claim_key(), false, "SmtpDeliveryFailed")).isTrue();
        jdbc.update("UPDATE accounts SET status='disabled' WHERE id=2");
        assertThatThrownBy(() -> service.resetPassword(resetToken(other), "new-password-123")).isInstanceOf(AuthException.class);
        assertThat(service.due(AccountActionEmailService.Purpose.RESET_PASSWORD)).isEmpty();
    }

    @Test void resendingResetInvalidatesOldTokenAndUnknownAccountsRemainPrivate() {
        service.request(AccountActionEmailService.Purpose.RESET_PASSWORD, "missing@example.com", "source");
        var first = resetClaim("a@example.com");
        service.request(AccountActionEmailService.Purpose.RESET_PASSWORD, "a@example.com", null);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM platform_account_action_emails", Long.class)).isEqualTo(1);
        jdbc.update("UPDATE platform_account_action_emails SET created_at=CURRENT_TIMESTAMP-INTERVAL '2 seconds'");
        var second = resetClaim("a@example.com");
        assertThatThrownBy(() -> service.resetPassword(resetToken(first), "new-password-123")).isInstanceOf(AuthException.class);
        assertThat(service.resetPassword(resetToken(second), "new-password-123")).isEqualTo(1);
    }

    @Test void previouslyIssuedResetCannotUndoAPasswordChange() {
        var claim = resetClaim("a@example.com");
        jdbc.update("UPDATE accounts SET password_hash='changed-by-another-path' WHERE id=1");
        assertThatThrownBy(() -> service.resetPassword(resetToken(claim), "new-password-123")).isInstanceOf(AuthException.class);
        assertThat(service.due(AccountActionEmailService.Purpose.RESET_PASSWORD)).isEmpty();
        assertThat(service.finish(AccountActionEmailService.Purpose.RESET_PASSWORD, claim.id(), claim.claim_key(), true, null)).isFalse();
    }

    @Test void sessionRevocationFailureRollsBackPasswordAndTokenConsumption() {
        var claim = resetClaim("a@example.com");
        jdbc.update("INSERT INTO auth_sessions (account_id) VALUES (1)");
        jdbc.execute("ALTER TABLE auth_sessions ADD CONSTRAINT simulate_revoke_failure CHECK (revoked_at IS NULL)");
        try {
            assertThatThrownBy(() -> service.resetPassword(resetToken(claim), "new-password-123"))
                .isInstanceOf(org.springframework.dao.DataIntegrityViolationException.class);
            assertThat(jdbc.queryForObject("SELECT password_hash FROM accounts WHERE id=1", String.class)).isEqualTo("old-hash");
            assertThat(jdbc.queryForObject("SELECT consumed_at FROM platform_account_action_emails WHERE id=?", java.sql.Timestamp.class, claim.id())).isNull();
        } finally {
            jdbc.execute("ALTER TABLE auth_sessions DROP CONSTRAINT simulate_revoke_failure");
        }
        assertThat(service.resetPassword(resetToken(claim), "new-password-123")).isEqualTo(1);
    }
}
