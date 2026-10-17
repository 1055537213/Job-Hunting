package com.jobhunting.platform.observability;

import java.util.List;
import java.util.Map;

import com.jobhunting.platform.auth.AccountActionEmailService;
import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.AuthException;
import com.jobhunting.platform.auth.PublicAuthErrorHandler;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import jakarta.servlet.http.Cookie;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import static com.jobhunting.platform.observability.ObservabilityFixtures.*;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

class AdminObservabilityControllerTest {
    final AccountAdministrationService accounts = mock(AccountAdministrationService.class);
    final RuntimeRequestMetrics runtime = mock(RuntimeRequestMetrics.class);
    final PythonObservabilityClient python = mock(PythonObservabilityClient.class);
    final AccountActionEmailService emails = mock(AccountActionEmailService.class);
    MockMvc mvc;

    @BeforeEach void setup() {
        var service = new AdminObservabilityService(accounts, runtime, python, emails, MAPPER);
        mvc = MockMvcBuilders.standaloneSetup(new AdminObservabilityController(service, new InternalTokenVerifier(TOKEN)))
                .setControllerAdvice(new PublicAuthErrorHandler()).build();
        when(accounts.authorizeAdministrator(nullable(String.class))).thenAnswer(call -> {
            if (!SESSION.equals(call.getArgument(0))) {
                throw new AuthException("SESSION_EXPIRED", "Session expired.", HttpStatus.UNAUTHORIZED);
            }
            return null;
        });
        when(runtime.snapshot()).thenReturn(MAPPER.convertValue(runtime(1, 100), Map.class));
        when(python.snapshot()).thenReturn(python(3, 30));
        when(emails.observations(any())).thenReturn(Map.of("summary", Map.of(), "records", List.of()));
    }

    @Test void publicGetUsesOnlyRealCookieAndNeverCaches() throws Exception {
        mvc.perform(get("/api/admin/observability/requests").cookie(new Cookie("job_agent_session", SESSION))
                .header("X-Account-ID", "999").param("session_token", "forged"))
                .andExpect(status().isOk()).andExpect(header().string("Cache-Control", "no-store"))
                .andExpect(jsonPath("$.requests.total_requests").value(4));
        verify(accounts).authorizeAdministrator(SESSION);
    }

    @Test void guestExpiredAndNonAdminCannotObserveOrFallBack() throws Exception {
        mvc.perform(get("/api/admin/observability/requests").header("X-Account-ID", "7").param("session_token", SESSION))
                .andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value("SESSION_EXPIRED"));
        mvc.perform(get("/api/admin/observability/requests").cookie(new Cookie("job_agent_session", "expired")))
                .andExpect(status().isUnauthorized());
        when(accounts.authorizeAdministrator(SESSION)).thenThrow(new AuthException("ADMIN_REQUIRED", "Administrator required.", HttpStatus.FORBIDDEN));
        mvc.perform(get("/api/admin/observability/requests").cookie(new Cookie("job_agent_session", SESSION)))
                .andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("ADMIN_REQUIRED"));
        verifyNoInteractions(runtime, python, emails);
    }

    @Test void compatibilityRequiresSharedTokenAndRealAdminSession() throws Exception {
        String body = "{\"session_token\":\"" + SESSION + "\"}";
        for (String token : new String[]{"", "wrong"}) {
            mvc.perform(post("/internal/v1/observability/requests").header("X-Internal-Service-Token", token)
                    .contentType("application/json").content(body)).andExpect(status().isUnauthorized())
                    .andExpect(jsonPath("$.code").value("INTERNAL_UNAUTHORIZED"));
        }
        verifyNoInteractions(accounts, runtime, python, emails);
        mvc.perform(post("/internal/v1/observability/requests").header("X-Internal-Service-Token", TOKEN)
                .contentType("application/json").content(body)).andExpect(status().isOk())
                .andExpect(header().string("Cache-Control", "no-store"));
        verify(accounts).authorizeAdministrator(SESSION);
        clearInvocations(runtime, python, emails);
        mvc.perform(post("/internal/v1/observability/requests").header("X-Internal-Service-Token", TOKEN)
                .contentType("application/json").content("{\"session_token\":\"expired\"}"))
                .andExpect(status().isUnauthorized());
        verifyNoInteractions(runtime, python, emails);
    }

    @Test void auth503PropagatesButPythonUnavailableReturnsPartial200() throws Exception {
        when(accounts.authorizeAdministrator(SESSION)).thenThrow(new AuthException("AUTH_UNAVAILABLE", "Authentication unavailable.", HttpStatus.SERVICE_UNAVAILABLE));
        mvc.perform(post("/internal/v1/observability/requests").header("X-Internal-Service-Token", TOKEN)
                .header("X-Request-ID", "request-id-123").contentType("application/json")
                .content("{\"session_token\":\"" + SESSION + "\"}"))
                .andExpect(status().isServiceUnavailable()).andExpect(jsonPath("$.code").value("AUTH_UNAVAILABLE"))
                .andExpect(jsonPath("$.request_id").value("request-id-123"));
        verifyNoInteractions(runtime, python, emails);
        doReturn(null).when(accounts).authorizeAdministrator(SESSION);
        var unavailable = ObservabilityProjection.unavailable(MAPPER);
        unavailable.set("account_email", ObservabilityProjection.unavailableEmail(MAPPER));
        when(python.snapshot()).thenReturn(unavailable);
        mvc.perform(get("/api/admin/observability/requests").cookie(new Cookie("job_agent_session", SESSION)))
                .andExpect(status().isOk()).andExpect(jsonPath("$.status").value("partial"))
                .andExpect(jsonPath("$.requests").isEmpty()).andExpect(jsonPath("$.account_email.summary").isEmpty());
    }

    @Test void malformedCompatibilityBodiesUseScopedValidationEnvelope() throws Exception {
        for (String body : new String[]{"{", "{}", "{\"session_token\":\"\"}"}) {
            mvc.perform(post("/internal/v1/observability/requests").header("X-Internal-Service-Token", TOKEN)
                    .contentType("application/json").content(body)).andExpect(status().is4xxClientError())
                    .andExpect(jsonPath("$.code").value("INVALID_REQUEST"));
        }
        assertThat(new AdminObservabilityController.SnapshotRequest(SESSION).toString()).doesNotContain(SESSION);
        verifyNoInteractions(accounts, runtime, python, emails);
    }
}
