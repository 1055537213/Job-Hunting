package com.jobhunting.platform.auth;

import java.util.List;
import java.util.Map;
import jakarta.servlet.http.Cookie;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@WebMvcTest(PublicAdminAccountController.class)
@Import({PublicAuthErrorHandler.class, PublicAdminAccountControllerTest.Tokens.class})
@TestPropertySource(properties = "platform.auth.enabled=true")
class PublicAdminAccountControllerTest {
    @Autowired MockMvc mvc;
    @MockitoBean AccountAdministrationService accounts;
    @MockitoBean AdminAuditService audit;

    @Test void guestsCannotListChangeStatusOrReadAudit() throws Exception {
        mvc.perform(get("/api/admin/accounts")).andExpect(status().isUnauthorized());
        mvc.perform(get("/api/admin/audit/events")).andExpect(status().isUnauthorized());
        mvc.perform(patch("/api/admin/accounts/2/status").contentType("application/json").content("{\"status\":\"disabled\"}"))
                .andExpect(status().isUnauthorized());
        verifyNoInteractions(accounts, audit);
    }

    @Test void listKeepsSafePublicAccountShape() throws Exception {
        when(accounts.list("session")).thenReturn(new AccountAdministrationService.AccountList(List.of(account())));
        mvc.perform(get("/api/admin/accounts").cookie(new Cookie("job_agent_session", "session")))
                .andExpect(status().isOk()).andExpect(jsonPath("$.accounts[0].id").value(2))
                .andExpect(jsonPath("$.accounts[0].account_id").doesNotExist())
                .andExpect(jsonPath("$.accounts[0].password_hash").doesNotExist())
                .andExpect(jsonPath("$.accounts[0].must_change_password").value(false));
    }

    @Test void statusUsesCookiePathAndNormalizedRequestIdNotClaimedActor() throws Exception {
        when(accounts.updateStatus("session", 2, "disabled", "status-trace"))
                .thenReturn(new AccountAdministrationService.AccountResponse(account()));
        mvc.perform(patch("/api/admin/accounts/2/status").cookie(new Cookie("job_agent_session", "session"))
                .header("X-Request-ID", "status-trace").contentType("application/json")
                .content("{\"status\":\"disabled\",\"actor_account_id\":999}"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.account.id").value(2));
        verify(accounts).updateStatus("session", 2, "disabled", "status-trace");
    }

    @Test void auditKeepsNullableFieldsAndDefaultsAndValidatesPagination() throws Exception {
        var event = new AdminAuditService.AuditEvent(3, null, null, "test", "account", null, "succeeded",
                "summary", Map.of(), null, "2026-10-16T00:00:00+00:00");
        when(audit.page("session", 50, 0)).thenReturn(new AdminAuditService.AuditPage(List.of(event), 50, 0));
        mvc.perform(get("/api/admin/audit/events").cookie(new Cookie("job_agent_session", "session")))
                .andExpect(status().isOk()).andExpect(jsonPath("$.limit").value(50))
                .andExpect(jsonPath("$.events[0].actor_account_id").value(org.hamcrest.Matchers.nullValue()))
                .andExpect(jsonPath("$.events[0].request_id").value(org.hamcrest.Matchers.nullValue()));
        clearInvocations(audit);
        for (String query : List.of("limit=0", "limit=201", "offset=-1", "limit=oops", "offset=9223372036854775808")) {
            mvc.perform(get("/api/admin/audit/events?" + query).cookie(new Cookie("job_agent_session", "session")))
                    .andExpect(status().isUnprocessableEntity()).andExpect(jsonPath("$.detail").isString());
        }
        verifyNoInteractions(audit);
    }

    @Test void internalAuditNeedsServiceTokenAndForwardsRealSession() throws Exception {
        String body = "{\"session_token\":\"session\",\"limit\":2,\"offset\":4,\"actor_account_id\":999}";
        mvc.perform(post("/internal/v1/auth/admin/audit/events").contentType("application/json").content(body))
                .andExpect(status().isUnauthorized());
        verifyNoInteractions(audit);
        mvc.perform(post("/internal/v1/auth/admin/audit/events").header("X-Internal-Service-Token", "admin-audit-secret")
                .contentType("application/json").content(body)).andExpect(status().isOk());
        verify(audit).page("session", 2, 4);
    }

    private AccountAdministrationService.AccountView account() {
        return new AccountAdministrationService.AccountView(2L, "user@example.com", null, "user", "disabled", false,
                null, null, "2026-10-16T00:00:00Z", "2026-10-16T00:00:00Z");
    }

    @TestConfiguration static class Tokens {
        @Bean InternalTokenVerifier verifier() { return new InternalTokenVerifier("admin-audit-secret"); }
    }
}
