package com.jobhunting.platform.observability;

import static com.jobhunting.platform.observability.RuntimeRequestMetricsTest.counts;
import static com.jobhunting.platform.observability.RuntimeRequestMetricsTest.errors;
import static com.jobhunting.platform.observability.RuntimeRequestMetricsTest.group;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.reset;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;
import static org.springframework.test.web.servlet.setup.MockMvcBuilders.standaloneSetup;

import java.util.Map;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.AuthException;
import com.jobhunting.platform.security.ClientAddressResolver;
import com.jobhunting.platform.security.RedisRequestLimiter;
import com.jobhunting.platform.security.WebSecurityFilter;
import com.jobhunting.platform.security.WebSecuritySettings;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.Cookie;
import jakarta.servlet.http.HttpServletResponse;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.HandlerMapping;
import tools.jackson.databind.ObjectMapper;

class RuntimeObservationFilterTest {
    final RuntimeRequestMetrics metrics = new RuntimeRequestMetrics();
    final RuntimeObservationFilter observation = new RuntimeObservationFilter(metrics);
    final AccountAdministrationService accounts = mock(AccountAdministrationService.class);
    final RedisRequestLimiter limiter = mock(RedisRequestLimiter.class);
    final SimpleMeterRegistry prometheus = new SimpleMeterRegistry();
    final ObjectMapper mapper = new ObjectMapper();
    MockMvc mvc;

    @BeforeEach void setup() {
        var settings = new WebSecuritySettings(true, "development", true, false, "http://localhost:8000",
                "test:runtime_limit", 60, 20, 120, 120, 240);
        var security = new WebSecurityFilter(accounts, limiter, settings, mapper, prometheus,
                new ClientAddressResolver(""));
        mvc = standaloneSetup(new Endpoints(metrics)).addFilters(observation)
                .addFilter(security, "/api/*").build();
    }

    void account() {
        when(accounts.current("secret-session")).thenReturn(new AccountAdministrationService.AccountResponse(
                new AccountAdministrationService.AccountView(2, "private@example.com", null, "user", "active", false,
                        "2026-10-12T00:00:00Z", null, "2026-10-12T00:00:00Z", "2026-10-12T00:00:00Z")));
    }

    @Test void realMvcUsesRouteTemplatesAndInternalPollNeverCountsAsApi() throws Exception {
        account();
        for (String id : new String[] {"private-account-1", "private-account-2"}) {
            mvc.perform(get("/api/items/" + id).queryParam("token", "private-query")
                    .cookie(new Cookie("job_agent_session", "secret-session"))).andExpect(status().isOk());
        }
        Map<String, Object> api = group(metrics, "api");
        assertThat(api).containsEntry("total_requests", 2L).containsEntry("in_flight_requests", 0L);
        assertThat(counts(api, "endpoint_counts")).containsExactlyEntriesOf(Map.of("/api/items/{id}", 2L));
        var poll = mvc.perform(get("/internal/runtime")).andExpect(status().isOk()).andReturn();
        var polled = mapper.readTree(poll.getResponse().getContentAsString());
        assertThat(polled.get("api").get("total_requests").asLong()).isEqualTo(2);
        assertThat(polled.get("internal").get("in_flight_requests").asLong()).isEqualTo(1);
        assertThat(group(metrics, "internal")).containsEntry("total_requests", 1L)
                .containsEntry("in_flight_requests", 0L);
        assertThat(group(metrics, "api")).containsEntry("total_requests", 2L);
        mvc.perform(get("/")).andExpect(status().isOk());
        mvc.perform(get("/actuator/health")).andExpect(status().isOk());
        assertThat(group(metrics, "api")).containsEntry("total_requests", 2L);
        assertThat(group(metrics, "internal")).containsEntry("total_requests", 1L);
        assertThat(mapper.writeValueAsString(metrics.snapshot())).doesNotContain(
                "private-account", "private-query", "secret-session", "private@example.com", "token=");
    }

    @Test void outerFilterSeesAuthCsrfRateLimitAndBackendRejectionsAndPreservesMicrometer() throws Exception {
        mvc.perform(get("/api/items/private-id").header("X-Request-ID", "request-observe-123"))
                .andExpect(status().isUnauthorized());
        when(limiter.check(anyString(), anyString())).thenReturn(11L);
        mvc.perform(post("/api/auth/login")).andExpect(status().isTooManyRequests());
        reset(limiter);
        account();
        mvc.perform(post("/api/items/private-id").cookie(new Cookie("job_agent_session", "secret-session")))
                .andExpect(status().isForbidden());
        mvc.perform(get("/api/admin/accounts").cookie(new Cookie("job_agent_session", "secret-session")))
                .andExpect(status().isForbidden());
        when(limiter.check(anyString(), anyString())).thenThrow(new RedisRequestLimiter.Unavailable());
        mvc.perform(post("/api/auth/login")).andExpect(status().isServiceUnavailable());
        reset(limiter);
        when(accounts.current(anyString())).thenThrow(new AuthException("SESSION_EXPIRED", "expired", HttpStatus.UNAUTHORIZED));
        mvc.perform(get("/api/items/private-id").cookie(new Cookie("job_agent_session", "secret-session")))
                .andExpect(status().isUnauthorized());
        reset(accounts);
        when(accounts.current(anyString())).thenThrow(new IllegalStateException("private-database-url"));
        mvc.perform(get("/api/auth/me").cookie(new Cookie("job_agent_session", "secret-session")))
                .andExpect(status().isServiceUnavailable());

        Map<String, Object> api = group(metrics, "api");
        assertThat(api).containsEntry("total_requests", 7L).containsEntry("error_requests", 7L)
                .containsEntry("rate_limited_requests", 1L).containsEntry("csrf_rejected_requests", 1L)
                .containsEntry("rate_limit_backend_errors", 1L).containsEntry("exception_requests", 1L)
                .containsEntry("in_flight_requests", 0L);
        assertThat(counts(api, "status_counts")).containsExactlyInAnyOrderEntriesOf(Map.of("4xx", 5L, "5xx", 2L));
        assertThat(counts(api, "outcome_counts")).containsEntry("unauthenticated", 1L)
                .containsEntry("auth_rejected", 1L).containsEntry("forbidden", 1L)
                .containsEntry("rate_limit_backend_unavailable", 1L);
        assertThat(counts(api, "endpoint_counts")).containsExactlyEntriesOf(Map.of("unmatched", 7L));
        assertThat(errors(api).getLast()).containsEntry("request_id", "request-observe-123");
        assertThat(mapper.writeValueAsString(metrics.snapshot())).doesNotContain("private-id", "private-database-url", "secret-session");
        assertThat(prometheus.find("platform.web.requests").counters().stream().mapToDouble(counter -> counter.count()).sum())
                .isEqualTo(7);
        assertThat(prometheus.find("platform.web.duration").timers().stream().mapToLong(timer -> timer.count()).sum())
                .isEqualTo(7);
    }

    @Test void ordinary503DoesNotPretendRateLimitBackendFailed() throws Exception {
        mvc.perform(get("/api/auth/verification/request")).andExpect(status().isServiceUnavailable());
        assertThat(group(metrics, "api")).containsEntry("error_requests", 1L)
                .containsEntry("rate_limit_backend_errors", 0L).containsEntry("exception_requests", 0L);
        assertThat(counts(group(metrics, "api"), "outcome_counts")).containsEntry("handled", 1L);
    }

    @Test void unmatchedRawPathsAndUnusualMethodsStayBoundedAndPrivate() throws Exception {
        for (int i = 0; i < 160; i++) {
            var request = new MockHttpServletRequest("PRIVATE-METHOD-" + i, "/api/private-" + i);
            request.setQueryString("password=private-query");
            request.setCookies(new Cookie("secret", "private-cookie"));
            request.addHeader("X-Request-ID", "bad\nprivate-id");
            observation.doFilter(request, new MockHttpServletResponse(), (req, res) ->
                    ((HttpServletResponse) res).setStatus(404));
        }
        Map<String, Object> api = group(metrics, "api");
        assertThat(counts(api, "endpoint_counts")).containsExactlyEntriesOf(Map.of("unmatched", 160L));
        assertThat(counts(api, "method_counts")).containsExactlyEntriesOf(Map.of("OTHER", 160L));
        assertThat(errors(api)).hasSize(20);
        assertThat(mapper.writeValueAsString(metrics.snapshot())).doesNotContain("private-", "password", "cookie");
    }

    @Test void thrownExceptionIsRecordedOnceAndRethrownWithoutItsMessage() throws Exception {
        var request = new MockHttpServletRequest("GET", "/internal/fail/private-id");
        var response = new MockHttpServletResponse();
        ServletException failure = new ServletException("private-exception-detail");
        assertThatThrownBy(() -> observation.doFilter(request, response, (req, res) -> {
            req.setAttribute(HandlerMapping.BEST_MATCHING_PATTERN_ATTRIBUTE, "/internal/fail/{id}");
            throw failure;
        })).isSameAs(failure);
        assertThat(group(metrics, "internal")).containsEntry("in_flight_requests", 0L)
                .containsEntry("total_requests", 1L).containsEntry("exception_requests", 1L)
                .containsEntry("error_requests", 1L);
        assertThat(errors(group(metrics, "internal")).getFirst()).containsEntry("status_code", 500)
                .containsEntry("endpoint", "/internal/fail/{id}");
        assertThat(mapper.writeValueAsString(metrics.snapshot())).doesNotContain("private-exception", "private-id");
        assertThat(response.getStatus()).isEqualTo(200);
    }

    @Test void asyncRequestsRemainInFlightUntilCompletionWithoutDoubleCounting() throws Exception {
        var request = new MockHttpServletRequest("GET", "/internal/poll");
        request.setAsyncSupported(true);
        var response = new MockHttpServletResponse();
        observation.doFilter(request, response, (req, res) -> {
            req.setAttribute(HandlerMapping.BEST_MATCHING_PATTERN_ATTRIBUTE, "/internal/poll");
            req.startAsync(req, res);
        });
        assertThat(group(metrics, "internal")).containsEntry("in_flight_requests", 1L)
                .containsEntry("total_requests", 0L);
        response.setStatus(503);
        request.getAsyncContext().complete();
        assertThat(group(metrics, "internal")).containsEntry("in_flight_requests", 0L)
                .containsEntry("total_requests", 1L).containsEntry("error_requests", 1L);
        request.getAsyncContext().complete();
        assertThat(group(metrics, "internal")).containsEntry("total_requests", 1L);
    }

    @Test void contextPathIsIgnoredWhenSelectingScope() throws Exception {
        var request = new MockHttpServletRequest("GET", "/platform/api/items/123");
        request.setContextPath("/platform");
        observation.doFilter(request, new MockHttpServletResponse(), (req, res) -> {});
        observation.doFilter(new MockHttpServletRequest("GET", "/apiary/private"),
                new MockHttpServletResponse(), (req, res) -> {});
        assertThat(group(metrics, "api")).containsEntry("total_requests", 1L);
    }

    @RestController
    static class Endpoints {
        final RuntimeRequestMetrics metrics;
        Endpoints(RuntimeRequestMetrics metrics) { this.metrics = metrics; }

        @GetMapping({"/api/items/{id}", "/api/auth/config", "/", "/actuator/health"})
        String ok() { return "ok"; }

        @GetMapping("/internal/runtime")
        Map<String, Object> runtime() { return metrics.snapshot(); }

        @GetMapping("/api/auth/verification/request")
        @ResponseStatus(HttpStatus.SERVICE_UNAVAILABLE)
        void unavailable() {}
    }
}
