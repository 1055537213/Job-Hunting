package com.jobhunting.platform.auth;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfSystemProperty;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import com.jobhunting.platform.usage.AdminUsageQueryService;
import com.jobhunting.platform.usage.AdminUsageService;
import com.jobhunting.platform.usage.AdminToolTraceService;
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
    @Autowired AdminAuditService audit;
    @Autowired AdminUsageQueryService usageQueries;
    @Autowired AdminUsageService usage;
    @Autowired AdminToolTraceService toolTraces;

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
              recipient_email varchar(254), target_email varchar(254), delivery_key varchar(64) UNIQUE, token_hash varchar(64) UNIQUE,
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
            CREATE TABLE IF NOT EXISTS usage_events (
              id serial PRIMARY KEY, account_id integer REFERENCES accounts(id), candidate_id integer,
              session_id text, root_request_id text, call_id text, provider text DEFAULT 'provider', model text DEFAULT 'model',
              operation text DEFAULT 'chat', input_tokens bigint DEFAULT 8, output_tokens bigint DEFAULT 2,
              total_tokens bigint DEFAULT 10, usage_source text DEFAULT 'provider', status text DEFAULT 'succeeded',
              attempt integer DEFAULT 1, provider_request_id text, raw_usage_json jsonb DEFAULT '{}',
              created_at timestamptz DEFAULT CURRENT_TIMESTAMP, billable boolean DEFAULT TRUE, pricing_version text)
            """);
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS tool_call_traces (id serial PRIMARY KEY, account_id integer REFERENCES accounts(id),
              candidate_id integer, session_id text, root_request_id varchar(128) UNIQUE, title text DEFAULT 'Task',
              status text, source text DEFAULT 'chat', step_count integer DEFAULT 0, attempt_count integer DEFAULT 0,
              last_step_name text, last_error_summary text, trace_json jsonb DEFAULT '{}',
              created_at timestamptz DEFAULT CURRENT_TIMESTAMP, started_at timestamptz, finished_at timestamptz,
              updated_at timestamptz DEFAULT CURRENT_TIMESTAMP)
            """);
        jdbc.execute("""
            CREATE TABLE IF NOT EXISTS account_balance_ledger (id serial PRIMARY KEY, account_id integer REFERENCES accounts(id))
            """);
        jdbc.execute("""
            TRUNCATE usage_events, tool_call_traces, account_balance_ledger,
                     platform_account_action_emails, account_email_outbox, account_action_tokens,
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
    void deletionCompletionIsIdempotentAndAnonymizesOnlyAfterAdmission() {
        var user = sessions.login("user@example.com", "password-123", false, null, null);

        assertThatThrownBy(() -> accounts.completeDeletion(2, "task-before-admission", "trace-before"))
            .isInstanceOf(AuthException.class)
            .satisfies(error -> assertThat(((AuthException) error).code()).isEqualTo("DELETION_NOT_ADMITTED"));

        accounts.prepareDeletion(user.session_token(), "password-123", "trace-admission");
        var completed = accounts.completeDeletion(2, "task-delete", "trace-complete");

        assertThat(completed.deleted()).isTrue();
        assertThat(jdbc.queryForObject("SELECT deleted_at IS NOT NULL FROM accounts WHERE id=2", Boolean.class))
            .isTrue();
        assertThat(jdbc.queryForObject("SELECT email FROM accounts WHERE id=2", String.class))
            .startsWith("deleted-2-");
        assertThat(jdbc.queryForObject(
                "SELECT count(*) FROM admin_audit_events WHERE action='account.deletion_completed' AND target_account_id=2",
                Integer.class)).isEqualTo(1);

        assertThat(accounts.completeDeletion(2, "task-delete", "trace-complete-retry").deleted()).isTrue();
        assertThat(jdbc.queryForObject(
                "SELECT count(*) FROM admin_audit_events WHERE action='account.deletion_completed' AND target_account_id=2",
                Integer.class)).isEqualTo(1);
    }

    @Test
    void profileUpdateUsesSessionAndWritesAuditAtomically() {
        var user = sessions.login("user@example.com", "password-123", false, null, null);

        var updated = accounts.updateProfile(user.session_token(), "Updated User", "trace-profile");

        assertThat(updated.account().display_name()).isEqualTo("Updated User");
        assertThat(jdbc.queryForObject(
                "SELECT display_name FROM accounts WHERE id=2", String.class))
            .isEqualTo("Updated User");
        assertThat(jdbc.queryForObject(
                "SELECT action FROM admin_audit_events WHERE target_account_id=2", String.class))
            .isEqualTo("account.profile_updated");
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

    @Test
    void auditReadsAllWritersWithStablePagesAndNullableLegacyFields() {
        String admin = sessions.login("admin@example.com", "password-123", false, null, null).session_token();
        accounts.updateStatus(admin, 2, "disabled", "status-trace");
        accounts.updateStatus(admin, 2, "active", "reactivate-trace");
        jdbc.update("""
            INSERT INTO admin_audit_events (action, target_type, outcome, summary, details_json, created_at)
            VALUES ('legacy.event', 'account', 'succeeded', 'legacy', '{}'::jsonb, CURRENT_TIMESTAMP)
            """);
        jdbc.update("UPDATE admin_audit_events SET created_at = '2026-10-16T00:00:00.123456+00:00'");
        var first = audit.page(admin, 2, 0);
        var second = audit.page(admin, 2, 2);
        assertThat(first.events()).extracting(AdminAuditService.AuditEvent::id).containsExactly(3L, 2L);
        assertThat(first.events().getFirst().actor_account_id()).isNull();
        assertThat(first.events().getFirst().target_account_id()).isNull();
        assertThat(first.events().getFirst().request_id()).isNull();
        assertThat(first.events().getFirst().created_at()).isEqualTo("2026-10-16T00:00:00.123456+00:00");
        assertThat(first.events().getLast().details()).containsEntry("next_status", "active");
        assertThat(second.events()).extracting(AdminAuditService.AuditEvent::request_id).containsExactly("status-trace");
        assertThat(audit.page(admin, 200, 3).events()).isEmpty();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM admin_audit_events", Integer.class)).isEqualTo(3);
    }

    @Test
    void auditRejectsUsersDisabledRevokedSessionsAndInvalidPages() {
        String admin = sessions.login("admin@example.com", "password-123", false, null, null).session_token();
        String user = sessions.login("user@example.com", "password-123", false, null, null).session_token();
        assertThatThrownBy(() -> audit.page(user, 50, 0)).isInstanceOf(AuthException.class)
                .extracting("code").isEqualTo("ADMIN_REQUIRED");
        assertThatThrownBy(() -> audit.page(admin, 201, 0)).isInstanceOf(AuthException.class)
                .extracting("code").isEqualTo("INVALID_REQUEST");
        jdbc.update("UPDATE accounts SET status='disabled' WHERE id=1");
        assertThatThrownBy(() -> audit.page(admin, 50, 0)).isInstanceOf(AuthException.class)
                .extracting("code").isEqualTo("ACCOUNT_DISABLED");
        jdbc.update("UPDATE accounts SET status='active' WHERE id=1");
        sessions.logout(admin);
        assertThatThrownBy(() -> audit.page(admin, 50, 0)).isInstanceOf(AuthException.class)
                .extracting("code").isEqualTo("SESSION_EXPIRED");
    }

    @Test
    void auditFailureRollsBackStatusAndSessionRevocation() {
        String admin = sessions.login("admin@example.com", "password-123", false, null, null).session_token();
        String user = sessions.login("user@example.com", "password-123", false, null, null).session_token();
        jdbc.execute("ALTER TABLE admin_audit_events ADD CONSTRAINT reject_status_audit_test CHECK (false) NOT VALID");
        try {
            assertThatThrownBy(() -> accounts.updateStatus(admin, 2, "disabled", "rollback-trace"))
                    .isInstanceOf(org.springframework.dao.DataIntegrityViolationException.class);
        } finally { jdbc.execute("ALTER TABLE admin_audit_events DROP CONSTRAINT reject_status_audit_test"); }
        assertThat(sessions.resolve(user)).isEqualTo(2L);
        assertThat(jdbc.queryForObject("SELECT status FROM accounts WHERE id=2", String.class)).isEqualTo("active");
        assertThat(audit.page(admin, 50, 0).events()).isEmpty();
    }

    @Test void usageReadsUsePerAccountWindowsBeforeFiltersAndKeepFinancialFacts() {
        String admin = sessions.login("admin@example.com", "password-123", false, null, null).session_token();
        jdbc.execute("INSERT INTO usage_events(account_id, candidate_id, call_id) SELECT 2, CASE WHEN n=1 THEN 7 ELSE 8 END, 'call-' || n FROM generate_series(1,501) n");
        jdbc.execute("INSERT INTO usage_events(account_id, call_id, raw_usage_json, billable) VALUES (1, 'admin-call', '{\"tokens\":10}', FALSE)");
        jdbc.execute("INSERT INTO tool_call_traces(account_id,status) SELECT 2, CASE WHEN n=1 THEN 'failed' ELSE 'completed' END FROM generate_series(1,501) n");
        jdbc.execute("INSERT INTO account_balance_ledger(account_id) SELECT 2 FROM generate_series(1,510)");
        var page = usage.events(admin, 2L, null, null, 100, 400);
        assertThat(page.total()).isEqualTo(500);
        assertThat(page.events()).hasSize(100);
        assertThat(page.events().getLast().call_id()).isEqualTo("call-2");
        assertThat(page.events().getFirst().candidate_id()).isEqualTo(8L);
        assertThat(usage.events(admin, 2L, 7L, null, 100, 0).events()).isEmpty();
        assertThat(usage.events(admin, 2L, null, "x' OR 1=1 --", 100, 0).total()).isZero();
        assertThat(usage.events(admin, 2L, null, null, 100, 500).events()).isEmpty();
        var summary = usage.summary(admin);
        var totals = (java.util.Map<?, ?>) summary.get("summary");
        assertThat(((Number) totals.get("event_count")).longValue()).isEqualTo(501);
        assertThat(((Number) totals.get("total_tokens")).longValue()).isEqualTo(5010);
        assertThat(((Number) totals.get("billable_tokens")).longValue()).isEqualTo(5000);
        var tools = (java.util.List<?>) summary.get("tool_calls_by_account");
        assertThat(((Number) ((java.util.Map<?, ?>) tools.getFirst()).get("failed_trace_count")).longValue()).isZero();
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM usage_events", Long.class)).isEqualTo(502);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM account_balance_ledger", Long.class)).isEqualTo(510);
    }

    @Test void usageSummaryReflectsNewWritesStatusThresholdAndDeletedAccountsWithoutCache() {
        String admin = sessions.login("admin@example.com", "password-123", false, null, null).session_token();
        jdbc.execute("UPDATE account_balances SET balance_micro_yuan=20000000 WHERE account_id=1");
        jdbc.execute("UPDATE account_balances SET balance_micro_yuan=5000000, low_balance_threshold_micro_yuan=6000000 WHERE account_id=2");
        var billing = (java.util.Map<?, ?>) usage.summary(admin).get("billing");
        var totals = (java.util.Map<?, ?>) billing.get("summary");
        assertThat(((Number) totals.get("healthy_account_count")).longValue()).isEqualTo(1);
        assertThat(((Number) totals.get("low_balance_account_count")).longValue()).isEqualTo(1);
        jdbc.execute("UPDATE accounts SET status='disabled' WHERE id=2");
        jdbc.execute("UPDATE account_balances SET total_consumed_micro_yuan=123 WHERE account_id=2");
        var fresh = (java.util.Map<?, ?>) ((java.util.Map<?, ?>) usage.summary(admin).get("billing")).get("summary");
        assertThat(((Number) fresh.get("suspended_account_count")).longValue()).isEqualTo(1);
        assertThat(((Number) fresh.get("total_consumed_micro_yuan")).longValue()).isEqualTo(123);
        jdbc.execute("UPDATE accounts SET deleted_at=CURRENT_TIMESTAMP WHERE id=2");
        var deleted = (java.util.Map<?, ?>) ((java.util.Map<?, ?>) usage.summary(admin).get("billing")).get("summary");
        assertThat(((Number) deleted.get("account_count")).longValue()).isEqualTo(1);
        jdbc.execute("DELETE FROM account_balances WHERE account_id=1");
        var missing = (java.util.Map<?, ?>) ((java.util.Map<?, ?>) usage.summary(admin).get("billing")).get("summary");
        assertThat(((Number) missing.get("total_balance_micro_yuan")).longValue()).isZero();
    }

    @Test void usageRejectsOrdinaryAndRevokedSessionsAndInvalidPages() {
        String user = sessions.login("user@example.com", "password-123", false, null, null).session_token();
        assertThatThrownBy(() -> usage.summary(user)).isInstanceOf(AuthException.class).extracting("code").isEqualTo("ADMIN_REQUIRED");
        String admin = sessions.login("admin@example.com", "password-123", false, null, null).session_token();
        assertThatThrownBy(() -> usage.events(admin, null, null, null, 101, 0)).isInstanceOf(AuthException.class);
        assertThatThrownBy(() -> usageQueries.events(null, null, null, 1, -1)).isInstanceOf(AuthException.class);
        sessions.logout(admin);
        assertThatThrownBy(() -> usage.summary(admin)).isInstanceOf(AuthException.class).extracting("code").isEqualTo("SESSION_EXPIRED");
    }

    @Test void toolTraceListAndDetailShareAccountRetentionAndStableOrder() {
        String admin = sessions.login("admin@example.com", "password-123", false, null, null).session_token();
        jdbc.execute("INSERT INTO tool_call_traces(account_id,root_request_id,status,updated_at) SELECT 2,'tool-' || n,'completed','2026-10-16T00:00:00.123456Z' FROM generate_series(1,501) n");
        jdbc.execute("INSERT INTO tool_call_traces(account_id,root_request_id,status) VALUES (1,'admin-tool','running')");
        var page = toolTraces.list(admin, 2L, 100, 400);
        assertThat(page.total()).isEqualTo(500);
        assertThat(page.traces()).hasSize(100);
        var last = page.traces().getLast();
        assertThat(last.get("root_request_id")).isEqualTo("tool-2");
        assertThat(last.get("updated_at")).isEqualTo("2026-10-16T00:00:00.123456+00:00");
        assertThat(last).hasSize(16).doesNotContainKeys("trace", "trace_json", "position");
        assertThat(last.get("candidate_id")).isNull();
        assertThat(last.get("started_at")).isNull();
        assertThat(toolTraces.list(admin, 2L, 100, 500).traces()).isEmpty();
        assertThat(toolTraces.list(admin, 999L, 1, 0).total()).isZero();
        assertThat(toolTraces.list(admin, null, 100, 0).total()).isEqualTo(501);
        assertThatThrownBy(() -> toolTraces.detail(admin, "tool-1")).isInstanceOf(AuthException.class).extracting("code").isEqualTo("TRACE_NOT_FOUND");
        assertThat(toolTraces.detail(admin, "tool-2")).containsKey("trace");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM tool_call_traces", Long.class)).isEqualTo(502);
    }

    @Test void toolTraceDetailsPreserveStepAndRetryOrderResultsErrorsAndUpdates() {
        String admin = sessions.login("admin@example.com", "password-123", false, null, null).session_token();
        String trace = """
            {"version":1,"status":"failed","steps":[
              {"name":"search_candidate_evidence","status":"completed","result":{"ok":true,"result_count":2}},
              {"name":"analyze_project","status":"failed","attempts":[
                {"attempt":1,"status":"failed","result":{"ok":false,"error":"timeout"}},
                {"attempt":2,"status":"failed","result":{"ok":false,"error":"provider unavailable"}}]}]}
            """;
        jdbc.update("""
            INSERT INTO tool_call_traces(account_id,root_request_id,status,step_count,attempt_count,last_step_name,last_error_summary,trace_json)
            VALUES (2,'detail-task','failed',2,3,'analyze_project','provider unavailable',CAST(? AS jsonb))
            """, trace);
        var detail = (java.util.Map<?, ?>) toolTraces.detail(admin, "detail-task").get("trace");
        assertThat(detail).hasSize(17);
        assertThat(detail.get("last_error_summary")).isEqualTo("provider unavailable");
        var snapshot = (java.util.Map<?, ?>) detail.get("trace");
        var steps = (java.util.List<?>) snapshot.get("steps");
        assertThat(((java.util.Map<?, ?>) steps.getFirst()).get("name")).isEqualTo("search_candidate_evidence");
        var retries = (java.util.List<?>) ((java.util.Map<?, ?>) steps.getLast()).get("attempts");
        var error = (java.util.Map<?, ?>) ((java.util.Map<?, ?>) retries.getLast()).get("result");
        assertThat(error.get("error")).isEqualTo("provider unavailable");
        jdbc.execute("UPDATE tool_call_traces SET status='waiting_confirmation', trace_json='{\"steps\":[],\"approval\":{\"status\":\"pending\"}}' WHERE root_request_id='detail-task'");
        assertThat(((java.util.Map<?, ?>) toolTraces.detail(admin, "detail-task").get("trace")).get("status")).isEqualTo("waiting_confirmation");
        jdbc.execute("UPDATE tool_call_traces SET trace_json='[]' WHERE root_request_id='detail-task'");
        assertThat(((java.util.Map<?, ?>) toolTraces.detail(admin, "detail-task").get("trace")).get("trace")).isEqualTo(java.util.Map.of());
        jdbc.execute("UPDATE tool_call_traces SET trace_json=NULL WHERE root_request_id='detail-task'");
        assertThat(((java.util.Map<?, ?>) toolTraces.detail(admin, "detail-task").get("trace")).get("trace")).isEqualTo(java.util.Map.of());
        assertThatThrownBy(() -> toolTraces.detail(admin, "x' OR 1=1 --")).isInstanceOf(AuthException.class).extracting("code").isEqualTo("TRACE_NOT_FOUND");
    }

    @Test void toolTraceReadsRejectUsersDisabledAndRevokedSessions() {
        String user = sessions.login("user@example.com", "password-123", false, null, null).session_token();
        assertThatThrownBy(() -> toolTraces.list(user, null, 100, 0)).isInstanceOf(AuthException.class).extracting("code").isEqualTo("ADMIN_REQUIRED");
        assertThatThrownBy(() -> toolTraces.detail(user, "missing")).isInstanceOf(AuthException.class).extracting("code").isEqualTo("ADMIN_REQUIRED");
        String admin = sessions.login("admin@example.com", "password-123", false, null, null).session_token();
        assertThatThrownBy(() -> toolTraces.list(admin, null, 101, 0)).isInstanceOf(AuthException.class);
        assertThatThrownBy(() -> toolTraces.list(admin, null, 1, -1)).isInstanceOf(AuthException.class);
        assertThatThrownBy(() -> toolTraces.detail(admin, "a".repeat(129))).isInstanceOf(AuthException.class);
        jdbc.execute("UPDATE accounts SET status='disabled' WHERE id=1");
        assertThatThrownBy(() -> toolTraces.detail(admin, "missing")).isInstanceOf(AuthException.class).extracting("code").isEqualTo("ACCOUNT_DISABLED");
        jdbc.execute("UPDATE accounts SET status='active' WHERE id=1");
        sessions.logout(admin);
        assertThatThrownBy(() -> toolTraces.list(admin, null, 100, 0)).isInstanceOf(AuthException.class).extracting("code").isEqualTo("SESSION_EXPIRED");
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
