package com.jobhunting.platform.auth;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.mock.web.MockHttpServletRequest;

class AuthErrorHandlerTest {
    @Test
    void internalErrorPreservesValidTraceIncludingColon() {
        var request = new MockHttpServletRequest();
        request.addHeader("X-Trace-Id", "trace:123");
        request.addHeader("X-Request-ID", "different-request");
        var error = new AuthException("SESSION_EXPIRED", "Session expired", HttpStatus.UNAUTHORIZED);

        var response = new AuthErrorHandler().authError(error, request);

        assertThat(response.getStatusCode()).isEqualTo(HttpStatus.UNAUTHORIZED);
        assertThat(response.getBody().trace_id()).isEqualTo("trace:123");
    }
}
