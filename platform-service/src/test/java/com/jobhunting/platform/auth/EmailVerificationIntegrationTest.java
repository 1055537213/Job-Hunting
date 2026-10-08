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
class EmailVerificationIntegrationTest {
    @Container static final PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:16-alpine");
    @DynamicPropertySource static void datasource(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", postgres::getJdbcUrl);
        registry.add("spring.datasource.username", postgres::getUsername);
        registry.add("spring.datasource.password", postgres::getPassword);
    }
    @Autowired EmailVerificationService service;
    @Autowired JdbcTemplate jdbc;

    @BeforeEach void reset() {
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS accounts (id integer PRIMARY KEY, email varchar(254) UNIQUE,
              status varchar(32), deleted_at timestamptz, email_verified_at timestamptz, updated_at timestamptz)
            """);
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS platform_email_verifications (
              id serial PRIMARY KEY, account_id integer REFERENCES accounts(id), recipient_email varchar(254),
              delivery_key varchar(64) UNIQUE, token_hash varchar(64) UNIQUE, request_source_hash varchar(64),
              expires_at timestamptz NOT NULL, consumed_at timestamptz, status varchar(32),
              attempt_count integer, max_attempts integer, next_attempt_at timestamptz,
              claimed_at timestamptz, claim_key varchar(64), sent_at timestamptz, last_error_type varchar(128),
              created_at timestamptz, updated_at timestamptz)
            """);
        jdbc.execute("TRUNCATE platform_email_verifications, accounts RESTART IDENTITY CASCADE");
        jdbc.update("INSERT INTO accounts (id,email,status) VALUES (1,'a@example.com','active'),(2,'b@example.com','active')");
    }
    private String token(EmailVerificationService.Claim claim) {
        return claim.action_url().split("verify_email_token=")[1];
    }
    @Test void verifiesExactlyOnceAndStoresOnlyDigest() throws Exception {
        service.request("A@example.com", null);
        var claim = service.claim(service.due().getFirst().id());
        String raw = token(claim);
        assertThat(jdbc.queryForObject("SELECT token_hash FROM platform_email_verifications", String.class)).hasSize(64).isNotEqualTo(raw);
        assertThat(service.claim(claim.id())).isNull();
        assertThat(service.finish(claim.id(), claim.claim_key(), true, null)).isTrue();
        var observations = service.observations();
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
        service.request("a@example.com", null);
        var claim = service.claim(service.due().getFirst().id());
        jdbc.update("UPDATE platform_email_verifications SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 minute'");
        assertThatThrownBy(() -> service.verify(token(claim))).isInstanceOf(AuthException.class);
        assertThat(service.due()).isEmpty();
        service.request("b@example.com", null);
        var other = service.claim(service.due().getFirst().id());
        jdbc.update("UPDATE accounts SET status='disabled' WHERE id=2");
        assertThatThrownBy(() -> service.verify(token(other))).isInstanceOf(AuthException.class);
        assertThat(service.finish(claim.id(), claim.claim_key(), true, null)).isFalse();
    }
    @Test void staleWorkersCannotOverwriteNewClaimAndFinalAttemptStops() {
        service.request("a@example.com", null);
        long id = service.due().getFirst().id();
        var first = service.claim(id);
        jdbc.update("UPDATE platform_email_verifications SET claimed_at=CURRENT_TIMESTAMP-INTERVAL '10 seconds'");
        var second = service.claim(id);
        assertThat(service.finish(id, first.claim_key(), true, null)).isFalse();
        assertThat(service.finish(id, second.claim_key(), false, "SmtpDeliveryFailed")).isTrue();
        assertThat(service.due()).isEmpty();
        assertThat(jdbc.queryForObject("SELECT status FROM platform_email_verifications", String.class)).isEqualTo("failed");
    }
    @Test void resendLimitsHideAccountExistenceAndPreserveLiveSentTokens() {
        service.request("missing@example.com", "source");
        service.request("a@example.com", "source");
        service.request("a@example.com", "source");
        service.request("b@example.com", "source");
        assertThat(service.due()).hasSize(1);
        var claim = service.claim(service.due().getFirst().id());
        service.finish(claim.id(), claim.claim_key(), true, null);
        jdbc.update("UPDATE platform_email_verifications SET updated_at=CURRENT_TIMESTAMP-INTERVAL '2 days'");
        service.due();
        assertThat(service.verify(token(claim))).isEqualTo(1L);
    }
}
