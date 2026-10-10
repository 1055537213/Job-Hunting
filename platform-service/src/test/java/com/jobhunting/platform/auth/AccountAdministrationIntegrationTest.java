package com.jobhunting.platform.auth;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

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
    "platform.auth.enabled=true", "platform.email.cooldown-seconds=1"
})
@Testcontainers
@EnabledIfSystemProperty(named = "run.integration.tests", matches = "true")
class AccountAdministrationIntegrationTest {
    @Container static final PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:16-alpine");

    @DynamicPropertySource
    static void datasource(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", postgres::getJdbcUrl);
        registry.add("spring.datasource.username", postgres::getUsername);
        registry.add("spring.datasource.password", postgres::getPassword);
    }

    @Autowired AccountAdministrationService accounts;
    @Autowired AccountActionEmailService emails;
    @Autowired JdbcTemplate jdbc;
    @Autowired PasswordHasher hasher;
    @Autowired SessionService sessions;

    @BeforeEach
    void reset() {
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
              id serial PRIMARY KEY, email varchar(254) UNIQUE NOT NULL, password_hash text NOT NULL,
              display_name varchar(128), role varchar(32) NOT NULL DEFAULT 'user', status varchar(32) NOT NULL,
              must_change_password boolean NOT NULL DEFAULT FALSE, email_verified_at timestamptz,
              deleted_at timestamptz, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL)
            """);
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS account_balances (
              account_id integer PRIMARY KEY REFERENCES accounts(id), balance_micro_yuan bigint NOT NULL,
              total_recharge_micro_yuan bigint NOT NULL, total_consumed_micro_yuan bigint NOT NULL,
              low_balance_threshold_micro_yuan bigint NOT NULL, created_at timestamptz, updated_at timestamptz)
            """);
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS account_consents (
              id serial PRIMARY KEY, account_id integer REFERENCES accounts(id), document_type varchar(32),
              version varchar(128), accepted_at timestamptz, ip_address varchar(64), user_agent varchar(512))
            """);
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS auth_sessions (
              id serial PRIMARY KEY, account_id integer REFERENCES accounts(id), token_hash varchar(64) UNIQUE,
              created_at timestamptz, last_seen_at timestamptz, expires_at timestamptz,
              absolute_expires_at timestamptz, revoked_at timestamptz, user_agent text, ip_address varchar(64))
            """);
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS platform_account_action_emails (
              id serial PRIMARY KEY, purpose varchar(32), account_id integer REFERENCES accounts(id),
              recipient_email varchar(254), delivery_key varchar(64) UNIQUE, token_hash varchar(64) UNIQUE,
              credential_hash varchar(64), request_source_hash varchar(64), expires_at timestamptz NOT NULL,
              consumed_at timestamptz, status varchar(32), attempt_count integer, max_attempts integer,
              next_attempt_at timestamptz, claimed_at timestamptz, claim_key varchar(64), sent_at timestamptz,
              last_error_type varchar(128), created_at timestamptz, updated_at timestamptz)
            """);
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS account_action_tokens (
              id serial PRIMARY KEY, account_id integer REFERENCES accounts(id), consumed_at timestamptz)
            """);
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS account_email_outbox (
              id serial PRIMARY KEY, account_id integer REFERENCES accounts(id), status varchar(32),
              claimed_at timestamptz, updated_at timestamptz)
            """);
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit_events (
              id serial PRIMARY KEY, actor_account_id integer REFERENCES accounts(id),
              target_account_id integer REFERENCES accounts(id), action varchar(96), target_type varchar(64),
              target_id varchar(160), outcome varchar(32), summary text, details_json jsonb,
              request_id varchar(128), created_at timestamptz)
            """);
        jdbc.execute("""
            TRUNCATE platform_account_action_emails, account_email_outbox, account_action_tokens,
                     admin_audit_events, auth_sessions, account_consents, account_balances, accounts
                     RESTART IDENTITY CASCADE
            """);
        insertAccount(1, "admin@example.com", "admin", "active");
        insertAccount(2, "user@example.com", "user", "active");
    }

    @Test
    void adminStatusChangeIsJavaOwnedAtomicAndRevokesSessions() {
        var admin = sessions.login("admin@example.com", "password-123", false, null, null);
        var user = sessions.login("user@example.com", "password-123", false, null, null);
        emails.request(AccountActionEmailService.Purpose.RESET_PASSWORD, "user@example.com", null);

        var listed = accounts.list(admin.session_token());
        assertThat(listed.accounts()).extracting(AccountAdministrationService.AccountView::email)
            .containsExactly("admin@example.com", "user@example.com");

        var disabled = accounts.updateStatus(admin.session_token(), 2, "disabled", "trace-status");
        assertThat(disabled.account().status()).isEqualTo("disabled");
        assertThat(sessions.resolve(user.session_token())).isNull();
        assertThat(jdbc.queryForObject(
                "SELECT count(*) FROM platform_account_action_emails WHERE account_id=2 AND consumed_at IS NOT NULL",
                Integer.class)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT details_json->>'next_status' FROM admin_audit_events", String.class))
            .isEqualTo("disabled");

        assertThatThrownBy(() -> accounts.list(user.session_token()))
            .isInstanceOf(AuthException.class)
            .satisfies(error -> assertThat(((AuthException) error).code()).isEqualTo("SESSION_EXPIRED"));
        accounts.updateStatus(admin.session_token(), 2, "active", "trace-reactivate");
        var fresh = sessions.login("user@example.com", "password-123", false, null, null);
        assertThat(sessions.resolve(fresh.session_token())).isEqualTo(2L);
        assertThat(sessions.resolve(user.session_token())).isNull();
    }

    @Test
    void policyProtectsSelfAndLastAdminAndBootstrapIsIdempotent() {
        var admin = sessions.login("admin@example.com", "password-123", false, null, null);
        assertThatThrownBy(() -> accounts.updateStatus(admin.session_token(), 1, "disabled", "trace-self"))
            .isInstanceOf(AuthException.class)
            .hasMessage("不能禁用当前正在使用的管理员账号。");

        assertThatThrownBy(() -> accounts.updateStatus(admin.session_token(), 1, "disabled", "trace-last"))
            .isInstanceOf(AuthException.class);

        jdbc.execute("TRUNCATE platform_account_action_emails, account_email_outbox, account_action_tokens, "
                + "admin_audit_events, auth_sessions, account_consents, account_balances, accounts RESTART IDENTITY CASCADE");
        var first = accounts.bootstrap("first-admin@example.com", "password-123", "First Admin");
        var repeated = accounts.bootstrap("second-admin@example.com", "different-password", "Second Admin");
        assertThat(first.created()).isTrue();
        assertThat(first.account_id()).isPositive();
        assertThat(repeated.created()).isFalse();
        assertThat(jdbc.queryForObject("SELECT role FROM accounts WHERE email='first-admin@example.com'", String.class))
            .isEqualTo("admin");
        assertThat(jdbc.queryForObject("SELECT balance_micro_yuan FROM account_balances", Long.class)).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM accounts", Integer.class)).isEqualTo(1);
    }

    private void insertAccount(int id, String email, String role, String status) {
        jdbc.update("""
            INSERT INTO accounts (id, email, password_hash, display_name, role, status,
                must_change_password, email_verified_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, FALSE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, id, email, hasher.encode("password-123"), email.split("@")[0], role, status);
        jdbc.update("""
            INSERT INTO account_balances (account_id, balance_micro_yuan, total_recharge_micro_yuan,
                total_consumed_micro_yuan, low_balance_threshold_micro_yuan, created_at, updated_at)
            VALUES (?, 0, 0, 0, 10000000, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, id);
    }
}
