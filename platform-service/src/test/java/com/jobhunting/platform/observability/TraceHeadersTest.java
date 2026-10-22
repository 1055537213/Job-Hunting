package com.jobhunting.platform.observability;

import static org.assertj.core.api.Assertions.assertThat;

import java.net.URI;
import java.net.http.HttpRequest;

import org.junit.jupiter.api.Test;

class TraceHeadersTest {
    @Test
    void preservesRequestCorrelationHeadersWithoutRequiringTraceBackend() {
        var request = TraceHeaders.apply(HttpRequest.newBuilder(URI.create("http://web/internal")), "trace-1234")
                .GET().build();

        assertThat(request.headers().firstValue("X-Request-ID")).contains("trace-1234");
        assertThat(request.headers().firstValue("X-Trace-Id")).contains("trace-1234");
        assertThat(request.headers().firstValue("traceparent")).isEmpty();
    }
}
