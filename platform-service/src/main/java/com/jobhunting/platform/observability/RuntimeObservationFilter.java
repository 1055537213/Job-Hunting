package com.jobhunting.platform.observability;

import java.io.IOException;
import java.util.concurrent.atomic.AtomicBoolean;

import com.jobhunting.platform.observability.RuntimeRequestMetrics.Scope;
import com.jobhunting.platform.security.WebSecurityFilter;
import jakarta.servlet.AsyncEvent;
import jakarta.servlet.AsyncListener;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.web.filter.OncePerRequestFilter;
import org.springframework.web.servlet.HandlerMapping;

public class RuntimeObservationFilter extends OncePerRequestFilter {
    private final RuntimeRequestMetrics metrics;

    public RuntimeObservationFilter(RuntimeRequestMetrics metrics) {
        this.metrics = metrics;
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        return scope(request) == null;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        Scope scope = scope(request);
        metrics.markStarted(scope);
        Observation observation = new Observation(scope, request, response);
        try {
            chain.doFilter(request, response);
        } catch (IOException | ServletException | RuntimeException | Error error) {
            observation.failed = true;
            throw error;
        } finally {
            if (request.isAsyncStarted()) {
                try {
                    request.getAsyncContext().addListener(observation);
                } catch (IllegalStateException completed) {
                    observation.finish();
                }
            } else {
                observation.finish();
            }
        }
    }

    private static Scope scope(HttpServletRequest request) {
        String path = request.getRequestURI().substring(request.getContextPath().length());
        if (path.startsWith("/api/")) return Scope.API;
        if (path.startsWith("/internal/")) return Scope.INTERNAL;
        return null;
    }

    private final class Observation implements AsyncListener {
        private final Scope scope;
        private final HttpServletRequest request;
        private final HttpServletResponse response;
        private final long started = System.nanoTime();
        private final String requestId;
        private final AtomicBoolean finished = new AtomicBoolean();
        private volatile boolean failed;

        Observation(Scope scope, HttpServletRequest request, HttpServletResponse response) {
            this.scope = scope;
            this.request = request;
            this.response = response;
            this.requestId = WebSecurityFilter.requestId(request.getHeader("X-Request-ID"), request.getHeader("X-Trace-Id"));
        }

        void finish() {
            if (!finished.compareAndSet(false, true)) return;
            int status = failed && response.getStatus() < 400 ? 500 : response.getStatus();
            Object pattern = request.getAttribute(HandlerMapping.BEST_MATCHING_PATTERN_ATTRIBUTE);
            String endpoint = pattern instanceof String template ? template : RuntimeRequestMetrics.UNMATCHED;
            Object attribute = request.getAttribute(WebSecurityFilter.OUTCOME_ATTRIBUTE);
            String outcome = failed ? "exception" : attribute instanceof String value ? value : "handled";
            String responseId = response.getHeader("X-Request-ID");
            metrics.record(scope, responseId == null ? requestId : responseId, request.getMethod(), endpoint,
                    status, (System.nanoTime() - started) / 1_000_000.0, outcome);
        }

        @Override public void onComplete(AsyncEvent event) { finish(); }
        @Override public void onError(AsyncEvent event) { failed = true; }
        @Override public void onTimeout(AsyncEvent event) { failed = true; }
        @Override public void onStartAsync(AsyncEvent event) { event.getAsyncContext().addListener(this); }
    }
}
