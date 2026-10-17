package com.jobhunting.platform.observability;

import static com.jobhunting.platform.observability.RuntimeRequestMetrics.Scope.API;
import static com.jobhunting.platform.observability.RuntimeRequestMetrics.Scope.INTERNAL;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.within;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;

class RuntimeRequestMetricsTest {
    final RuntimeRequestMetrics metrics = new RuntimeRequestMetrics();

    @SuppressWarnings("unchecked")
    static Map<String, Object> group(RuntimeRequestMetrics metrics, String name) {
        return (Map<String, Object>) metrics.snapshot().get(name);
    }

    @SuppressWarnings("unchecked")
    static Map<String, Long> counts(Map<String, Object> snapshot, String name) {
        return (Map<String, Long>) snapshot.get(name);
    }

    @SuppressWarnings("unchecked")
    static List<Map<String, Object>> errors(Map<String, Object> snapshot) {
        return (List<Map<String, Object>>) snapshot.get("recent_errors");
    }

    void record(double duration) {
        metrics.markStarted(API);
        metrics.record(API, "request-test-123", "GET", "/api/items/{id}", 200, duration, "handled");
    }

    @Test void exposesSixteenFieldsAndJacksonSerializableUtcEnvelope() throws Exception {
        Map<String, Object> snapshot = metrics.snapshot();
        assertThat(snapshot).containsOnlyKeys("status", "api", "internal", "observed_at");
        assertThat(snapshot.get("status")).isEqualTo("available");
        assertThat(Instant.parse((String) snapshot.get("observed_at"))).isNotNull();
        Map<String, Object> api = group(metrics, "api");
        assertThat(api).containsOnlyKeys("started_at", "in_flight_requests", "total_requests", "total_duration_ms",
                "error_requests", "average_duration_ms", "max_duration_ms", "rate_limited_requests",
                "rate_limit_backend_errors", "csrf_rejected_requests", "exception_requests", "status_counts",
                "method_counts", "endpoint_counts", "outcome_counts", "recent_errors");
        assertThat(Instant.parse((String) api.get("started_at"))).isNotNull();
        assertThat(new ObjectMapper().readTree(new ObjectMapper().writeValueAsString(snapshot))
                .get("api").get("total_requests").asLong()).isZero();
    }

    @Test void frameworkFallbackPatternsDoNotInvalidateTheServiceProjection() {
        for (String endpoint : List.of("/**", "/api/**", "/error", "/api/private?token=secret")) {
            metrics.markStarted(API);
            metrics.record(API, "request-fallback-123", "GET", endpoint, 404, 1, "handled");
        }
        assertThat(counts(group(metrics, "api"), "endpoint_counts")).containsExactlyEntriesOf(Map.of("unmatched", 4L));
        assertThat(ObservabilityProjection.runtime(new ObjectMapper(), new ObjectMapper().valueToTree(metrics.snapshot()), false)
                .path("status").asText()).isEqualTo("available");
    }

    @Test void averagesRawDurationsAcrossAllRequestsAndNewInstanceResetsBothScopes() {
        record(0.004);
        record(0.004);
        record(100.002);
        assertThat(group(metrics, "api")).containsEntry("total_requests", 3L)
                .containsEntry("average_duration_ms", 33.34)
                .containsEntry("max_duration_ms", 100.002).containsEntry("in_flight_requests", 0L);
        assertThat((double) group(metrics, "api").get("total_duration_ms")).isCloseTo(100.01, within(1e-9));
        metrics.markStarted(INTERNAL);
        metrics.record(INTERNAL, "request-test-456", "GET", "/internal/runtime", 503, 10, "handled");
        RuntimeRequestMetrics restarted = new RuntimeRequestMetrics();
        for (String scope : List.of("api", "internal")) {
            assertThat(group(restarted, scope)).containsEntry("total_requests", 0L)
                    .containsEntry("in_flight_requests", 0L).containsEntry("total_duration_ms", 0.0)
                    .containsEntry("average_duration_ms", 0.0).containsEntry("max_duration_ms", 0.0);
            assertThat(counts(group(restarted, scope), "endpoint_counts")).isEmpty();
            assertThat(errors(group(restarted, scope))).isEmpty();
        }
    }

    @Test void boundsEndpointsMethodsOutcomesAndKeepsOnlyTwentyNewestSafeErrors() {
        for (int i = 0; i < 180; i++) {
            metrics.markStarted(API);
            metrics.record(API, "bad\nrequest", "CUSTOM-" + i, "/api/route-" + i + "/{id}",
                    404, i, "unsafe-outcome-" + i);
        }
        Map<String, Object> api = group(metrics, "api");
        Map<String, Long> endpoints = counts(api, "endpoint_counts");
        assertThat(endpoints).hasSize(129).containsEntry("other", 52L);
        assertThat(endpoints.values().stream().mapToLong(Long::longValue).sum()).isEqualTo(180);
        assertThat(counts(api, "method_counts")).containsExactlyEntriesOf(Map.of("OTHER", 180L));
        assertThat(counts(api, "outcome_counts")).containsExactlyEntriesOf(Map.of("handled", 180L));
        List<Map<String, Object>> errors = errors(api);
        assertThat(errors).hasSize(20);
        assertThat(errors.getFirst()).containsEntry("duration_ms", 179.0);
        assertThat(errors.getLast()).containsEntry("duration_ms", 160.0);
        assertThat(errors).allSatisfy(error -> {
            assertThat(error).containsOnlyKeys("request_id", "method", "endpoint", "status_code", "outcome",
                    "duration_ms", "created_at");
            assertThat((String) error.get("request_id")).matches("[A-Za-z0-9._-]{8,128}");
            assertThat(error.get("endpoint")).isEqualTo("other");
            assertThat(Instant.parse((String) error.get("created_at"))).isNotNull();
        });
    }

    @Test void snapshotsCannotMutateLiveTotalsOrChangeEarlierSnapshots() {
        record(12);
        Map<String, Object> first = group(metrics, "api");
        counts(first, "method_counts").clear();
        first.put("total_requests", 200L);
        record(24);
        assertThat(group(metrics, "api")).containsEntry("total_requests", 2L);
        assertThat(counts(group(metrics, "api"), "method_counts")).containsEntry("GET", 2L);
        assertThat(counts(first, "endpoint_counts")).containsEntry("/api/items/{id}", 1L);
    }

    @Test void concurrentStartsCompletionsAndSnapshotsKeepExactTotalsAndMax() throws Exception {
        int workers = 12;
        int perWorker = 250;
        CountDownLatch started = new CountDownLatch(workers);
        CountDownLatch release = new CountDownLatch(1);
        try (var executor = Executors.newFixedThreadPool(workers)) {
            var futures = new java.util.ArrayList<java.util.concurrent.Future<?>>();
            for (int worker = 0; worker < workers; worker++) {
                futures.add(executor.submit(() -> {
                    metrics.markStarted(API);
                    started.countDown();
                    try {
                        if (!release.await(10, TimeUnit.SECONDS)) throw new AssertionError("release timed out");
                        metrics.record(API, "request-test-123", "GET", "/api/items/{id}", 200, 1.25, "handled");
                        for (int i = 1; i < perWorker; i++) {
                            record(1.25);
                            Map<String, Object> snapshot = group(metrics, "api");
                            long total = (long) snapshot.get("total_requests");
                            assertThat(snapshot.get("total_duration_ms")).isEqualTo(total * 1.25);
                            assertThat(counts(snapshot, "status_counts").get("2xx")).isEqualTo(total);
                        }
                    } catch (InterruptedException error) {
                        Thread.currentThread().interrupt();
                        throw new AssertionError(error);
                    }
                }));
            }
            try {
                assertThat(started.await(10, TimeUnit.SECONDS)).isTrue();
                assertThat(group(metrics, "api")).containsEntry("in_flight_requests", (long) workers)
                        .containsEntry("total_requests", 0L);
            } finally {
                release.countDown();
            }
            for (var future : futures) future.get(20, TimeUnit.SECONDS);
        }
        assertThat(group(metrics, "api")).containsEntry("total_requests", (long) workers * perWorker)
                .containsEntry("total_duration_ms", workers * perWorker * 1.25)
                .containsEntry("average_duration_ms", 1.25).containsEntry("max_duration_ms", 1.25)
                .containsEntry("in_flight_requests", 0L);
        assertThat(group(metrics, "internal")).containsEntry("total_requests", 0L);
    }
}
