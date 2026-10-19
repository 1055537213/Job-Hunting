package com.jobhunting.platform.profiles;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.PublicAuthErrorHandler;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import com.jobhunting.platform.security.ClientAddressResolver;
import com.jobhunting.platform.security.RedisRequestLimiter;
import com.jobhunting.platform.security.WebSecurityFilter;
import com.jobhunting.platform.security.WebSecuritySettings;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import jakarta.servlet.http.Cookie;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.test.web.servlet.MockMvc;
import tools.jackson.databind.ObjectMapper;

import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import static org.springframework.test.web.servlet.setup.MockMvcBuilders.standaloneSetup;

class ProfileCreateControllerTest {
    final AccountAdministrationService accounts = mock(AccountAdministrationService.class);
    final ProfileQueryClient python = mock(ProfileQueryClient.class);
    final RedisRequestLimiter limiter = mock(RedisRequestLimiter.class);
    final ObjectMapper mapper = new ObjectMapper();
    final Cookie session = new Cookie("job_agent_session", "real-session");
    final Cookie csrf = new Cookie("job_agent_csrf", "csrf-value");
    MockMvc mvc;

    @BeforeEach void setup() {
        when(accounts.current("real-session")).thenReturn(new AccountAdministrationService.AccountResponse(
                new AccountAdministrationService.AccountView(42L, "owner@example.com", "Owner", "user", "active", false, null, null, "now", "now")));
        when(python.create(anyString(), anyLong(), anyString(), any(), anyString()))
                .thenReturn(mapper.createObjectNode().put("candidate_id", 7).set("profile", ProfileQueryClientTest.profile()));
        var filter = new WebSecurityFilter(accounts, limiter,
                new WebSecuritySettings(true, "development", true, true, "http://localhost:8000", "test:create", 60, 20, 120, 120, 240),
                mapper, new SimpleMeterRegistry(), new ClientAddressResolver(""));
        mvc = standaloneSetup(new ProfileCreateController(accounts, python, new InternalTokenVerifier("service-secret")))
                .setControllerAdvice(new PublicAuthErrorHandler()).addFilter(filter, "/api/*").build();
    }

    @Test void requiresSessionCsrfAndValidKeyBeforeCallback() throws Exception {
        mvc.perform(post("/api/profiles").contentType("application/json").content("{\"name\":\"Candidate\"}"))
                .andExpect(status().isUnauthorized());
        mvc.perform(post("/api/profiles").cookie(session).contentType("application/json").content("{\"name\":\"Candidate\"}"))
                .andExpect(status().isForbidden());
        mvc.perform(post("/api/profiles").cookie(session, csrf).header("X-CSRF-Token", "csrf-value")
                .header("Idempotency-Key", "invalid key").contentType("application/json").content("{\"name\":\"Candidate\"}"))
                .andExpect(status().isUnprocessableContent());
        verifyNoInteractions(python);
    }

    @Test void ownerComesFromSessionAndRetriesKeepKeyAndTrace() throws Exception {
        for (int i = 0; i < 2; i++) {
            mvc.perform(post("/api/profiles?account_id=99").cookie(session, csrf).header("X-CSRF-Token", "csrf-value")
                    .header("Idempotency-Key", "stable-intent").header("X-Request-ID", "creation-trace")
                    .contentType("application/json").content("{\"name\":\"Candidate\"}"))
                    .andExpect(status().isOk()).andExpect(jsonPath("$.candidate_id").value(7))
                    .andExpect(header().string("Cache-Control", "no-store"));
        }
        verify(python, times(2)).create("real-session", 42, "stable-intent", mapper.readTree("{\"name\":\"Candidate\"}"), "creation-trace");
        mvc.perform(post("/api/profiles").cookie(session, csrf).header("X-CSRF-Token", "csrf-value")
                .contentType("application/json").content("{\"name\":\"Candidate\",\"account_id\":99}"))
                .andExpect(status().isUnprocessableContent());
    }

    @Test void internalEntryNeedsServiceAndSessionAndDependencyFailureIsNotSuccess() throws Exception {
        String body = "{\"session_token\":\"real-session\",\"idempotency_key\":\"stable-intent\",\"profile\":{\"name\":\"Candidate\"}}";
        mvc.perform(post("/internal/v1/profiles/create").contentType("application/json").content(body)).andExpect(status().isUnauthorized());
        verifyNoInteractions(python);
        mvc.perform(post("/internal/v1/profiles/create").header("X-Internal-Service-Token", "service-secret")
                .contentType("application/json").content(body)).andExpect(status().isOk());
        when(python.create(anyString(), anyLong(), anyString(), any(), anyString())).thenThrow(ProfileQueryClient.createUnavailable());
        mvc.perform(post("/api/profiles").cookie(session, csrf).header("X-CSRF-Token", "csrf-value")
                .contentType("application/json").content("{\"name\":\"Candidate\"}"))
                .andExpect(status().isServiceUnavailable()).andExpect(jsonPath("$.code").value("PROFILE_CREATE_UNAVAILABLE"))
                .andExpect(header().string("Retry-After", "3"));
    }
}
