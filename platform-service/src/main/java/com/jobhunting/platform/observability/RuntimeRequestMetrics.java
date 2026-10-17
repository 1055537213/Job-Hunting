package com.jobhunting.platform.observability;

import java.time.Instant;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

import com.jobhunting.platform.security.WebSecurityFilter;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

@Component
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class RuntimeRequestMetrics {
    static final String UNMATCHED = "unmatched";
    static final String OTHER = "other";
    private static final int ENDPOINT_LIMIT = 128;
    private static final int ERROR_LIMIT = 20;
    private static final Set<String> METHODS = Set.of(
            "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE");
    private static final Set<String> OUTCOMES = Set.of(
            "handled", "invalid_path", "invalid_address", "rate_limited", "unauthenticated",
            "forbidden", "csrf_rejected", "auth_rejected", "rate_limit_backend_unavailable", "exception");

    enum Scope { API, INTERNAL }

    private final String startedAt = Instant.now().toString();
    private final Totals api = new Totals();
    private final Totals internal = new Totals();

    public synchronized Map<String, Object> snapshot() {
        return Map.of("status", "available", "api", snapshot(api), "internal", snapshot(internal),
                "observed_at", Instant.now().toString());
    }

    synchronized void markStarted(Scope scope) {
        totals(scope).inFlight++;
    }

    synchronized void record(Scope scope, String requestId, String method, String endpoint,
            int statusCode, double durationMs, String outcome) {
        Totals totals = totals(scope);
        String normalizedMethod = method == null ? "OTHER" : method.toUpperCase(Locale.ROOT);
        if (!METHODS.contains(normalizedMethod)) normalizedMethod = "OTHER";
        String normalizedOutcome = "protection_unavailable".equals(outcome)
                ? "rate_limit_backend_unavailable" : outcome;
        if (normalizedOutcome == null || !OUTCOMES.contains(normalizedOutcome)
                || "handled".equals(normalizedOutcome)) {
            normalizedOutcome = switch (statusCode) {
                case 401 -> "unauthenticated";
                case 403 -> "forbidden";
                case 429 -> "rate_limited";
                default -> "handled";
            };
        }
        String normalizedEndpoint = endpoint == null || endpoint.isBlank() ? UNMATCHED : endpoint;
        String prefix = scope == Scope.API ? "/api/" : "/internal/";
        if (!Set.of(UNMATCHED, OTHER).contains(normalizedEndpoint)
                && (!normalizedEndpoint.startsWith(prefix) || normalizedEndpoint.length() > 256
                || !normalizedEndpoint.matches("/[A-Za-z0-9_/{}/.:-]+"))) {
            normalizedEndpoint = UNMATCHED;
        }
        int endpointCount = totals.endpoints.size() - (totals.endpoints.containsKey(OTHER) ? 1 : 0);
        if (!totals.endpoints.containsKey(normalizedEndpoint) && endpointCount >= ENDPOINT_LIMIT) {
            normalizedEndpoint = OTHER;
        }
        double duration = Double.isFinite(durationMs) ? Math.max(0, durationMs) : 0;
        int status = Math.max(100, Math.min(599, statusCode));
        String family = (status / 100) + "xx";
        totals.inFlight = Math.max(0, totals.inFlight - 1);
        totals.requests++;
        totals.durationMs += duration;
        totals.maxDurationMs = Math.max(totals.maxDurationMs, duration);
        totals.statuses.merge(family, 1L, Long::sum);
        totals.methods.merge(normalizedMethod, 1L, Long::sum);
        totals.endpoints.merge(normalizedEndpoint, 1L, Long::sum);
        totals.outcomes.merge(normalizedOutcome, 1L, Long::sum);
        if (status >= 400 || Set.of("rate_limited", "csrf_rejected", "exception").contains(normalizedOutcome)) {
            totals.errors.addFirst(Map.of(
                    "request_id", WebSecurityFilter.requestId(requestId, null),
                    "method", normalizedMethod, "endpoint", normalizedEndpoint, "status_code", status,
                    "outcome", normalizedOutcome, "duration_ms", duration, "created_at", Instant.now().toString()));
            if (totals.errors.size() > ERROR_LIMIT) totals.errors.removeLast();
        }
    }

    private Totals totals(Scope scope) {
        return scope == Scope.API ? api : internal;
    }

    private Map<String, Object> snapshot(Totals totals) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("started_at", startedAt);
        result.put("in_flight_requests", totals.inFlight);
        result.put("total_requests", totals.requests);
        result.put("total_duration_ms", totals.durationMs);
        result.put("error_requests", totals.statuses.getOrDefault("4xx", 0L)
                + totals.statuses.getOrDefault("5xx", 0L));
        result.put("average_duration_ms", totals.requests == 0 ? 0.0
                : Math.round(totals.durationMs / totals.requests * 100.0) / 100.0);
        result.put("max_duration_ms", totals.maxDurationMs);
        result.put("rate_limited_requests", totals.outcomes.getOrDefault("rate_limited", 0L));
        result.put("rate_limit_backend_errors", totals.outcomes.getOrDefault("rate_limit_backend_unavailable", 0L));
        result.put("csrf_rejected_requests", totals.outcomes.getOrDefault("csrf_rejected", 0L));
        result.put("exception_requests", totals.outcomes.getOrDefault("exception", 0L));
        result.put("status_counts", new TreeMap<>(totals.statuses));
        result.put("method_counts", new TreeMap<>(totals.methods));
        result.put("endpoint_counts", new TreeMap<>(totals.endpoints));
        result.put("outcome_counts", new TreeMap<>(totals.outcomes));
        result.put("recent_errors", new ArrayList<>(totals.errors));
        return result;
    }

    private static final class Totals {
        long inFlight;
        long requests;
        double durationMs;
        double maxDurationMs;
        final Map<String, Long> statuses = new TreeMap<>();
        final Map<String, Long> methods = new TreeMap<>();
        final Map<String, Long> endpoints = new TreeMap<>();
        final Map<String, Long> outcomes = new TreeMap<>();
        final ArrayDeque<Map<String, Object>> errors = new ArrayDeque<>();
    }
}
