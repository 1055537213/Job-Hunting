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
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import static org.springframework.test.web.servlet.setup.MockMvcBuilders.standaloneSetup;

class AccountExportControllerTest {
    final AccountAdministrationService accounts = mock(AccountAdministrationService.class);
    final AccountExportClient python = mock(AccountExportClient.class);
    final RedisRequestLimiter limiter = mock(RedisRequestLimiter.class);
    final Cookie cookie = new Cookie("job_agent_session", "real-session");
    MockMvc mvc;

    @BeforeEach void setup() {
        when(accounts.current("real-session")).thenReturn(new AccountAdministrationService.AccountResponse(
                new AccountAdministrationService.AccountView(42L, "owner@example.com", "Owner", "user", "active", false, null, null, "now", "now")));
        when(python.export("real-session", 42)).thenReturn(new AccountExportClientTest().fixture());
        var service = new AccountExportService(accounts, python);
        var filter = new WebSecurityFilter(accounts, limiter,
                new WebSecuritySettings(true, "development", true, false, "http://localhost:8000", "test:export", 60, 20, 120, 120, 240),
                new ObjectMapper(), new SimpleMeterRegistry(), new ClientAddressResolver(""));
        mvc = standaloneSetup(new PublicAccountExportController(service), new InternalAccountExportController(service, new InternalTokenVerifier("service-secret")))
                .setControllerAdvice(new PublicAuthErrorHandler()).addFilter(filter, "/api/*").build();
    }

    @Test void sessionDeterminesOwnerNotRequestParametersAndHeadersAreLocal() throws Exception {
        mvc.perform(get("/api/account/export?account_id=999").cookie(cookie))
                .andExpect(status().isOk()).andExpect(jsonPath("$.account.id").value(42))
                .andExpect(header().string("Cache-Control", "no-store"))
                .andExpect(header().string("Content-Disposition", "attachment; filename=\"job-agent-account-42-export.json\""))
                .andExpect(header().doesNotExist("Set-Cookie"));
        verify(python).export("real-session", 42);
    }

    @Test void guestsRevokedSessionsAndLimiterCannotReachPython() throws Exception {
        mvc.perform(get("/api/account/export")).andExpect(status().isUnauthorized());
        when(accounts.current("real-session")).thenThrow(new AuthException("SESSION_EXPIRED", "expired", HttpStatus.UNAUTHORIZED));
        mvc.perform(get("/api/account/export").cookie(cookie)).andExpect(status().isUnauthorized());
        when(limiter.check(anyString(), anyString())).thenReturn(3L);
        mvc.perform(get("/api/account/export").cookie(cookie)).andExpect(status().isTooManyRequests());
        verifyNoInteractions(python);
    }

    @Test void compatibilityInternalEntryRequiresBothCredentialsAndUsesSameService() throws Exception {
        mvc.perform(post("/internal/v1/accounts/export").contentType("application/json").content("{\"session_token\":\"real-session\"}"))
                .andExpect(status().isUnauthorized());
        verifyNoInteractions(python);
        mvc.perform(post("/internal/v1/accounts/export").header("X-Internal-Service-Token", "service-secret")
                .contentType("application/json").content("{\"session_token\":\"real-session\",\"account_id\":999}"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.account.id").value(42))
                .andExpect(header().string("Cache-Control", "no-store"));
        verify(python).export("real-session", 42);
    }

    @Test void upstreamUnavailableNeverFallsBackAndDoesNotProduceAnAttachment() throws Exception {
        when(python.export("real-session", 42)).thenThrow(AccountExportClient.unavailable());
        mvc.perform(get("/api/account/export").cookie(cookie)).andExpect(status().isServiceUnavailable())
                .andExpect(header().string("Retry-After", "3")).andExpect(header().doesNotExist("Content-Disposition"))
                .andExpect(jsonPath("$.code").value("EXPORT_UNAVAILABLE"));
        assertThat(new InternalAccountExportController.ExportRequest("private-session").toString()).doesNotContain("private-session");
    }
}
