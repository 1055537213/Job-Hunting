package com.jobhunting.platform.profiles;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.AuthException;
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
import org.springframework.http.HttpStatus;
import org.springframework.test.web.servlet.MockMvc;
import tools.jackson.databind.ObjectMapper;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import static org.springframework.test.web.servlet.setup.MockMvcBuilders.standaloneSetup;

class ProfileReadControllerTest {
    final AccountAdministrationService accounts = mock(AccountAdministrationService.class);
    final ProfileQueryClient python = mock(ProfileQueryClient.class);
    final RedisRequestLimiter limiter = mock(RedisRequestLimiter.class);
    final ObjectMapper mapper = new ObjectMapper();
    final Cookie cookie = new Cookie("job_agent_session", "real-session");
    MockMvc mvc;

    @BeforeEach void setup() {
        when(accounts.current("real-session")).thenReturn(new AccountAdministrationService.AccountResponse(
                new AccountAdministrationService.AccountView(42L, "owner@example.com", "Owner", "user", "active", false, null, null, "now", "now")));
        var list = mapper.createObjectNode();
        list.putArray("profiles").add(ProfileQueryClientTest.profile());
        when(python.query(eq("real-session"), eq(42L), isNull(), anyString())).thenReturn(list);
        when(python.query(eq("real-session"), eq(42L), eq(7L), anyString())).thenReturn(mapper.createObjectNode().set("profile", ProfileQueryClientTest.profile()));
        var filter = new WebSecurityFilter(accounts, limiter,
                new WebSecuritySettings(true, "development", true, false, "http://localhost:8000", "test:profile", 60, 20, 120, 120, 240),
                mapper, new SimpleMeterRegistry(), new ClientAddressResolver(""));
        mvc = standaloneSetup(new ProfileReadController(new ProfileReadService(accounts, python), new InternalTokenVerifier("service-secret")))
                .setControllerAdvice(new PublicAuthErrorHandler()).addFilter(filter, "/api/*").build();
    }

    @Test void ownerComesOnlyFromSessionAndClientGetsOriginalContractWithNoCache() throws Exception {
        mvc.perform(get("/api/profiles?account_id=999").cookie(cookie).header("X-Request-ID", "profile-read-trace"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.profiles[0].id").value(7))
                .andExpect(jsonPath("$.account_id").doesNotExist()).andExpect(header().string("Cache-Control", "no-store"));
        verify(python).query("real-session", 42, null, "profile-read-trace");
        mvc.perform(get("/api/profiles/7?account_id=999").cookie(cookie)).andExpect(status().isOk()).andExpect(jsonPath("$.profile.id").value(7));
    }

    @Test void unauthenticatedRevokedAndRateLimitedRequestsCannotReadPython() throws Exception {
        mvc.perform(get("/api/profiles")).andExpect(status().isUnauthorized());
        when(accounts.current("real-session")).thenThrow(new AuthException("SESSION_EXPIRED", "expired", HttpStatus.UNAUTHORIZED));
        mvc.perform(get("/api/profiles/7").cookie(cookie)).andExpect(status().isUnauthorized());
        when(limiter.check(anyString(), anyString())).thenReturn(3L);
        mvc.perform(get("/api/profiles").cookie(cookie)).andExpect(status().isTooManyRequests());
        verifyNoInteractions(python);
    }

    @Test void callbackErrorsAndInvalidPathAreNotSuccessfulEmptyLists() throws Exception {
        when(python.query(anyString(), anyLong(), any(), anyString())).thenThrow(ProfileQueryClient.unavailable());
        mvc.perform(get("/api/profiles").cookie(cookie)).andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.code").value("PROFILE_QUERY_UNAVAILABLE")).andExpect(header().string("Retry-After", "3"));
        for (String id : new String[]{"0", "-1", "not-a-number", "9223372036854775808"}) {
            mvc.perform(get("/api/profiles/" + id).cookie(cookie)).andExpect(status().isUnprocessableContent());
        }
    }

    @Test void internalCompatibilityEntryRequiresServiceAndSessionAndRedactsDto() throws Exception {
        String body = "{\"session_token\":\"real-session\",\"candidate_id\":7,\"account_id\":999}";
        mvc.perform(post("/internal/v1/profiles/query").contentType("application/json").content(body)).andExpect(status().isUnauthorized());
        verifyNoInteractions(python);
        mvc.perform(post("/internal/v1/profiles/query").header("X-Internal-Service-Token", "service-secret")
                .header("X-Trace-Id", "profile-internal-trace").contentType("application/json").content(body))
                .andExpect(status().isOk()).andExpect(jsonPath("$.profile.id").value(7));
        verify(python).query("real-session", 42, 7L, "profile-internal-trace");
        mvc.perform(post("/internal/v1/profiles/query").header("X-Internal-Service-Token", "service-secret")
                .contentType("application/json").content("{\"session_token\":\"private-session\",\"candidate_id\":0}"))
                .andExpect(status().isUnprocessableContent()).andExpect(content().string(org.hamcrest.Matchers.not(org.hamcrest.Matchers.containsString("private-session"))));
        assertThat(new ProfileReadController.Query("private-session", 7L).toString()).doesNotContain("private-session");
    }
}
