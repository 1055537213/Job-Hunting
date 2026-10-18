package com.jobhunting.platform.auth;

import com.jobhunting.platform.billing.InternalTokenVerifier;
import com.jobhunting.platform.security.ClientAddressResolver;
import com.jobhunting.platform.security.RedisRequestLimiter;
import com.jobhunting.platform.security.WebSecurityFilter;
import com.jobhunting.platform.security.WebSecuritySettings;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import jakarta.servlet.http.Cookie;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.test.web.servlet.MockMvc;
import tools.jackson.databind.ObjectMapper;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import static org.springframework.test.web.servlet.setup.MockMvcBuilders.standaloneSetup;

class AccountDeletionControllerTest {
    final AccountAdministrationService accounts = mock(AccountAdministrationService.class);
    final AccountDeletionRequests requests = mock(AccountDeletionRequests.class);
    final RedisRequestLimiter limiter = mock(RedisRequestLimiter.class);
    final Cookie session = new Cookie("job_agent_session", "real-session");
    final Cookie csrf = new Cookie("job_agent_csrf", "csrf-secret");
    MockMvc mvc;
    final String body = "{\"current_password\":\"private-password\",\"confirmation\":\"注销账号\",\"account_id\":999}";

    @BeforeEach void setup() {
        var account = new AccountAdministrationService.AccountView(42L, "owner@example.com", null, "user", "active", false, null, null, "now", "now");
        when(accounts.current("real-session")).thenReturn(new AccountAdministrationService.AccountResponse(account));
        when(accounts.prepareDeletion(eq("real-session"), eq("private-password"), anyString()))
                .thenReturn(new AccountAdministrationService.AccountResponse(account));
        when(requests.forAccount(42)).thenReturn(new AccountDeletionRequests.Request(42, "request-key", "task-key", "trace-delete"));
        var filter = new WebSecurityFilter(accounts, limiter,
                new WebSecuritySettings(true, "development", true, false, "http://localhost:8000", "test:delete", 60, 20, 120, 120, 240),
                new ObjectMapper(), new SimpleMeterRegistry(), new ClientAddressResolver(""));
        mvc = standaloneSetup(new AccountDeletionController(accounts, requests, new InternalTokenVerifier("service-secret"), true))
                .setControllerAdvice(new PublicAuthErrorHandler()).addFilter(filter, "/api/*").build();
    }

    @Test void admissionReturns202AndExpiresCookiesOnlyAfterTransaction() throws Exception {
        mvc.perform(post("/api/account/delete").cookie(session, csrf).header("X-CSRF-Token", "csrf-secret")
                .header("X-Request-ID", "trace-delete").contentType("application/json").content(body))
                .andExpect(status().isAccepted()).andExpect(jsonPath("$.status").value("accepted"))
                .andExpect(jsonPath("$.task_key").value("task-key"))
                .andExpect(header().string("Cache-Control", "no-store"))
                .andExpect(cookie().maxAge("job_agent_session", 0)).andExpect(cookie().maxAge("job_agent_csrf", 0))
                .andExpect(cookie().secure("job_agent_session", true));
        verify(accounts).prepareDeletion("real-session", "private-password", "trace-delete");
        verify(requests).forAccount(42);
    }

    @Test void guestsAndCsrfAndInvalidConfirmationNeverAdmit() throws Exception {
        mvc.perform(post("/api/account/delete").contentType("application/json").content(body)).andExpect(status().isUnauthorized());
        mvc.perform(post("/api/account/delete").cookie(session).contentType("application/json").content(body)).andExpect(status().isForbidden());
        mvc.perform(post("/api/account/delete").cookie(session, csrf).header("X-CSRF-Token", "csrf-secret")
                .contentType("application/json").content(body.replace("注销账号", "wrong"))).andExpect(status().isBadRequest());
        verify(accounts, never()).prepareDeletion(any(), any(), any());
        verifyNoInteractions(requests);
    }

    @Test void passwordAndAdminRejectionNeverClearCookiesOrFetchRequests() throws Exception {
        for (HttpStatus status : new HttpStatus[] {HttpStatus.BAD_REQUEST, HttpStatus.FORBIDDEN}) {
            doThrow(new AuthException("DELETION_REJECTED", "rejected", status)).when(accounts).prepareDeletion(any(), any(), any());
            mvc.perform(post("/api/account/delete").cookie(session, csrf).header("X-CSRF-Token", "csrf-secret")
                    .contentType("application/json").content(body)).andExpect(status().is(status.value()))
                    .andExpect(header().doesNotExist("Set-Cookie"));
        }
        verifyNoInteractions(requests);
    }

    @Test void internalDispatchRequiresServiceTokenAndLimitIsBounded() throws Exception {
        mvc.perform(post("/internal/v1/accounts/deletions/due").contentType("application/json").content("{\"limit\":20}"))
                .andExpect(status().isUnauthorized());
        mvc.perform(post("/internal/v1/accounts/deletions/due").header("X-Internal-Service-Token", "service-secret")
                .contentType("application/json").content("{\"limit\":21}")).andExpect(status().isUnprocessableEntity());
        when(requests.due(20)).thenReturn(java.util.List.of(new AccountDeletionRequests.Request(42, "request-key", "task-key", "trace-delete")));
        mvc.perform(post("/internal/v1/accounts/deletions/due").header("X-Internal-Service-Token", "service-secret")
                .contentType("application/json").content("{\"limit\":20}")).andExpect(status().isOk())
                .andExpect(jsonPath("$.requests[0].account_id").value(42)).andExpect(jsonPath("$.requests[0].session_token").doesNotExist());
        assertThat(new AccountDeletionController.InternalDeleteRequest("session", "password", "confirm").toString())
                .doesNotContain("session", "password", "confirm");
    }
}
