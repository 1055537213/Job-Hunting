package com.jobhunting.platform.observability;

import java.net.http.HttpRequest;

import io.opentelemetry.api.trace.propagation.W3CTraceContextPropagator;
import io.opentelemetry.context.Context;

/** Adds legacy correlation headers and the standard W3C trace context. */
public final class TraceHeaders {
    private TraceHeaders() { }

    public static HttpRequest.Builder apply(HttpRequest.Builder builder, String requestId) {
        if (requestId != null && !requestId.isBlank()) {
            builder.header("X-Request-ID", requestId);
            builder.header("X-Trace-Id", requestId);
        }
        W3CTraceContextPropagator.getInstance().inject(
                Context.current(), builder, (carrier, key, value) -> carrier.header(key, value));
        return builder;
    }
}
