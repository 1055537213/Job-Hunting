package com.jobhunting.platform.security;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.AuthException;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import jakarta.servlet.http.Cookie;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import tools.jackson.databind.ObjectMapper;

class WebSecurityFilterTest {
    final AccountAdministrationService accounts = mock(AccountAdministrationService.class);
    final RedisRequestLimiter limiter = mock(RedisRequestLimiter.class);
    final SimpleMeterRegistry metrics = new SimpleMeterRegistry();
    WebSecurityFilter filter;
    boolean called;

    static WebSecuritySettings settings() {
        return new WebSecuritySettings(true, "development", true, false, "http://localhost:8000",
                "test:platform_limit", 60, 20, 120, 120, 240);
    }

    @BeforeEach void setup() {
        filter = new WebSecurityFilter(accounts, limiter, settings(), new ObjectMapper(), metrics, new ClientAddressResolver(""));
        called = false;
    }

    MockHttpServletResponse request(String path, String method, boolean session, boolean csrf) throws Exception {
        var request = new MockHttpServletRequest(method, path);
        request.addHeader("X-Request-ID", "request-test-123");
        request.setRemoteAddr("127.0.0.1");
        request.addHeader("X-Forwarded-For", "spoofed-ip");
        if (session) request.setCookies(new Cookie("job_agent_session", "test-session"), new Cookie("job_agent_csrf", "csrf"));
        if (csrf) request.addHeader("X-CSRF-Token", "csrf");
        var response = new MockHttpServletResponse();
        filter.doFilter(request, response, (req, res) -> {
            called = true;
            assertThat(((jakarta.servlet.http.HttpServletRequest) req).getHeader("X-Trace-Id")).isEqualTo("request-test-123");
        });
        assertThat(response.getHeader("X-Frame-Options")).isEqualTo("DENY");
        assertThat(response.getHeader("Cache-Control")).isEqualTo("no-store");
        return response;
    }

    void account(String role) {
        when(accounts.current("test-session")).thenReturn(new AccountAdministrationService.AccountResponse(
                new AccountAdministrationService.AccountView(2, "u@example.com", null, role, "active", false,
                        "2026-10-12T00:00:00Z", null, "2026-10-12T00:00:00Z", "2026-10-12T00:00:00Z")));
    }

    @Test void anonymousLoginIsLimitedBeforePasswordOrDatabaseAccess() throws Exception {
        assertThat(request("/api/auth/login", "POST", false, false).getStatus()).isEqualTo(200);
        assertThat(called).isTrue();
        verify(limiter).check("ip:127.0.0.1", "auth");
        verifyNoInteractions(accounts);
    }

    @Test void rejectsExcessAndDoesNotTrustForwardedIdentity() throws Exception {
        when(limiter.check("ip:127.0.0.1", "auth")).thenReturn(17L);
        var response = request("/api/auth/login", "POST", false, false);
        assertThat(response.getStatus()).isEqualTo(429);
        assertThat(response.getHeader("Retry-After")).isEqualTo("17");
        assertThat(response.getContentAsString()).contains("RATE_LIMITED", "request-test-123");
        assertThat(called).isFalse();
        verifyNoInteractions(accounts);
    }

    @Test void redisFailureFailsClosedWithoutDatabaseFallback() throws Exception {
        when(limiter.check(anyString(), anyString())).thenThrow(new RedisRequestLimiter.Unavailable());
        assertThat(request("/api/account/password", "POST", true, true).getStatus()).isEqualTo(503);
        verifyNoInteractions(accounts);
        assertThat(called).isFalse();
    }

    @Test void protectedAndAdminRoutesEnforceRealSessionAndRole() throws Exception {
        assertThat(request("/api/admin/accounts", "GET", false, false).getStatus()).isEqualTo(401);
        account("user");
        assertThat(request("/api/admin/accounts", "GET", true, false).getStatus()).isEqualTo(403);
        account("admin");
        assertThat(request("/api/admin/accounts", "GET", true, false).getStatus()).isEqualTo(200);
        assertThat(called).isTrue();
        verify(limiter, times(2)).check("account:2", "admin");
    }

    @Test void csrfAndExpiredSessionDoNotReachHandler() throws Exception {
        account("user");
        assertThat(request("/api/account/password", "POST", true, false).getStatus()).isEqualTo(403);
        assertThat(called).isFalse();
        assertThat(request("/api/account/password", "POST", true, true).getStatus()).isEqualTo(200);
        called = false;
        when(accounts.current(anyString())).thenThrow(new AuthException("SESSION_EXPIRED", "expired", HttpStatus.UNAUTHORIZED));
        assertThat(request("/api/account/password", "POST", true, true).getStatus()).isEqualTo(401);
        assertThat(called).isFalse();
    }

    @Test void anonymousLogoutIsIdempotentButOtherPathsRemainProtected() throws Exception {
        assertThat(request("/api/auth/logout", "POST", false, false).getStatus()).isEqualTo(200);
        called = false;
        assertThat(request("/api/auth/new-protected-route", "POST", false, false).getStatus()).isEqualTo(401);
        assertThat(called).isFalse();
    }

    @Test void databaseFailureIsUnavailableNotAnonymousAndMetricsAreLowCardinality() throws Exception {
        when(accounts.current(anyString())).thenThrow(new IllegalStateException("secret database URL"));
        var response = request("/api/auth/me", "GET", true, false);
        assertThat(response.getStatus()).isEqualTo(503);
        assertThat(response.getContentAsString()).doesNotContain("secret", "test-session");
        assertThat(metrics.getMeters()).allSatisfy(meter ->
                assertThat(meter.getId().getTags()).allSatisfy(tag ->
                        assertThat(tag.getKey()).isIn("group", "status", "outcome")));
    }

    @Test void encodedAdminPathsAndMalformedIdsCannotBypassProtection() throws Exception {
        assertThat(request("/api/%61dmin/accounts", "GET", true, false).getStatus()).isEqualTo(400);
        assertThat(called).isFalse();
        assertThat(WebSecurityFilter.requestId("bad\ninput", null)).matches("[A-Za-z0-9._-]{8,128}");
        assertThat(WebSecurityFilter.requestId(null, "trace-test-123")).isEqualTo("trace-test-123");
    }

    @Test void productionCannotDisableRequiredProtections() {
        assertThatThrownBy(() -> new WebSecurityConfiguration(settings(), "", "")
                .clientAddressResolver("", "framework")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new WebSecuritySettings(false, "production", true, true,
                "https://example.com", "test", 60, 20, 120, 120, 240)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new WebSecuritySettings(false, " PRODUCTION ", true, true,
                "https://example.com", "test", 60, 20, 120, 120, 240)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new WebSecuritySettings(true, "production", false, true,
                "https://example.com", "test", 60, 20, 120, 120, 240)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new WebSecuritySettings(true, "production", true, false,
                "http://example.com", "test", 60, 20, 120, 120, 240)).isInstanceOf(IllegalArgumentException.class);
        var production = new WebSecuritySettings(true, "production", true, true,
                "https://example.com", "test", 60, 20, 120, 120, 240);
        assertThatThrownBy(() -> new WebSecurityConfiguration(production, "", "short"))
                .isInstanceOf(IllegalArgumentException.class);
    }
}
