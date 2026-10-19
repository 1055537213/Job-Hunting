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

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import static org.springframework.test.web.servlet.setup.MockMvcBuilders.standaloneSetup;

class ProfileDeleteControllerTest {
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
        when(python.delete(anyString(), anyLong(), anyLong(), anyString())).thenReturn(mapper.createObjectNode().put("candidate_id", 7)
                .put("deleted", true).put("cleanup_status", "pending").put("rag_cleanup", "database_cascade"));
        var filter = new WebSecurityFilter(accounts, limiter,
                new WebSecuritySettings(true, "development", true, true, "http://localhost:8000", "test:delete", 60, 20, 120, 120, 240),
                mapper, new SimpleMeterRegistry(), new ClientAddressResolver(""));
        mvc = standaloneSetup(new ProfileDeleteController(new ProfileReadService(accounts, python), new InternalTokenVerifier("service-secret")))
                .setControllerAdvice(new PublicAuthErrorHandler()).addFilter(filter, "/api/*").build();
    }

    @Test void sessionCsrfPositiveLocatorAndRateLimitAreRequired() throws Exception {
        mvc.perform(delete("/api/profiles/7")).andExpect(status().isUnauthorized());
        mvc.perform(delete("/api/profiles/7").cookie(session)).andExpect(status().isForbidden());
        for (String id : new String[]{"0", "-1", "x", "9223372036854775808"}) {
            mvc.perform(delete("/api/profiles/" + id).cookie(session, csrf).header("X-CSRF-Token", "csrf-value"))
                    .andExpect(status().isUnprocessableContent());
        }
        when(limiter.check(anyString(), anyString())).thenReturn(3L);
        mvc.perform(delete("/api/profiles/7").cookie(session, csrf).header("X-CSRF-Token", "csrf-value")).andExpect(status().isTooManyRequests());
        verifyNoInteractions(python);
    }

    @Test void pendingIsAcceptedNotCompletedAndCallerCannotChooseOwner() throws Exception {
        mvc.perform(delete("/api/profiles/7?account_id=99").cookie(session, csrf).header("X-CSRF-Token", "csrf-value")
                .header("X-Request-ID", "deletion-trace")).andExpect(status().isAccepted())
                .andExpect(jsonPath("$.deleted").value(true)).andExpect(jsonPath("$.cleanup_status").value("pending"))
                .andExpect(header().string("Cache-Control", "no-store"));
        verify(python).delete("real-session", 42, 7, "deletion-trace");
        when(python.delete(anyString(), anyLong(), anyLong(), anyString())).thenThrow(ProfileQueryClient.deleteUnavailable());
        mvc.perform(delete("/api/profiles/7").cookie(session, csrf).header("X-CSRF-Token", "csrf-value"))
                .andExpect(status().isServiceUnavailable()).andExpect(jsonPath("$.code").value("PROFILE_DELETE_UNAVAILABLE"))
                .andExpect(header().string("Retry-After", "3"));
    }

    @Test void internalCompatibilityRequiresTokenAndRealSessionAndRedactsDto() throws Exception {
        String body = "{\"session_token\":\"real-session\",\"candidate_id\":7}";
        mvc.perform(post("/internal/v1/profiles/delete").contentType("application/json").content(body)).andExpect(status().isUnauthorized());
        verifyNoInteractions(python);
        mvc.perform(post("/internal/v1/profiles/delete").header("X-Internal-Service-Token", "service-secret")
                .contentType("application/json").content(body)).andExpect(status().isOk()).andExpect(jsonPath("$.cleanup_status").value("pending"));
        assertThat(new ProfileDeleteController.Command("private-session", 7L).toString()).doesNotContain("private-session");
    }
}
