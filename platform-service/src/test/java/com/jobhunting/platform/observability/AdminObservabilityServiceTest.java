package com.jobhunting.platform.observability;

import java.util.List;
import java.util.Map;

import com.jobhunting.platform.auth.AccountActionEmailService;
import com.jobhunting.platform.auth.AccountAdministrationService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

import static com.jobhunting.platform.observability.ObservabilityFixtures.*;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class AdminObservabilityServiceTest {
    final AccountAdministrationService accounts = mock(AccountAdministrationService.class);
    final RuntimeRequestMetrics runtime = mock(RuntimeRequestMetrics.class);
    final PythonObservabilityClient python = mock(PythonObservabilityClient.class);
    final AccountActionEmailService emails = mock(AccountActionEmailService.class);
    final AdminObservabilityService service = new AdminObservabilityService(accounts, runtime, python, emails, MAPPER);

    @BeforeEach void setup() {
        when(runtime.snapshot()).thenReturn(MAPPER.convertValue(runtime(1, 100), Map.class));
        when(python.snapshot()).thenReturn(python(3, 30));
        when(emails.observations(any())).thenReturn(Map.of("summary", Map.of(), "records", List.of()));
    }

    @Test void mergesApiOnlyWithWeightedAverageAndEndpointProvenance() {
        var result = service.snapshot(SESSION);
        var metrics = result.get("requests");
        assertThat(result.path("status").asText()).isEqualTo("available");
        assertThat(metrics.path("total_requests").asLong()).isEqualTo(4);
        assertThat(metrics.path("total_duration_ms").asDouble()).isEqualTo(130);
        assertThat(metrics.path("average_duration_ms").asDouble()).isEqualTo(32.5);
        assertThat(metrics.path("max_duration_ms").asDouble()).isEqualTo(100);
        assertThat(metrics.path("method_counts").path("GET").asLong()).isEqualTo(4);
        assertThat(metrics.path("endpoint_counts").path("java:/api/health").asLong()).isEqualTo(1);
        assertThat(metrics.path("endpoint_counts").path("python:/api/health").asLong()).isEqualTo(3);
        assertThat(result.path("services").path("python").has("account_email")).isFalse();
        assertThat(result.path("services").path("java").path("internal").path("total_requests").asLong()).isEqualTo(100);
        verify(accounts).authorizeAdministrator(SESSION);
    }

    @Test void zeroSnapshotsAreAvailableAndMissingMetricsAreUnknown() {
        when(runtime.snapshot()).thenReturn(MAPPER.convertValue(runtime(0, 0), Map.class));
        when(python.snapshot()).thenReturn(python(0, 0));
        var result = service.snapshot(SESSION);
        assertThat(result.path("status").asText()).isEqualTo("available");
        assertThat(result.path("requests").path("average_duration_ms").asDouble()).isZero();
        var unavailable = ObservabilityProjection.unavailable(MAPPER);
        unavailable.set("account_email", ObservabilityProjection.unavailableEmail(MAPPER));
        when(python.snapshot()).thenReturn(unavailable);
        result = service.snapshot(SESSION);
        assertThat(result.path("status").asText()).isEqualTo("partial");
        assertThat(result.path("requests").isNull()).isTrue();
        assertThat(result.path("account_email").path("summary").isNull()).isTrue();
        verify(emails, times(2)).observations(AccountActionEmailService.Purpose.VERIFY_EMAIL);
    }

    @Test void aggregateRetainsBothFullEndpointMaps() {
        var local = runtime(1, 100);
        var remote = python(3, 30);
        var localEndpoints = (ObjectNode) local.get("api").get("endpoint_counts");
        var remoteEndpoints = (ObjectNode) remote.get("api").get("endpoint_counts");
        localEndpoints.removeAll();
        remoteEndpoints.removeAll();
        for (int i = 0; i < 130; i++) {
            localEndpoints.put("/api/route_" + i, 1);
            remoteEndpoints.put("/api/route_" + i, 2);
        }
        when(runtime.snapshot()).thenReturn(MAPPER.convertValue(local, Map.class));
        when(python.snapshot()).thenReturn(ObservabilityProjection.runtime(MAPPER, remote, true));
        assertThat(service.snapshot(SESSION).path("requests").path("endpoint_counts").size()).isEqualTo(260);
    }

    @Test void emailFailureDoesNotHideAvailableRequestMetricsOrOtherSafeRecords() {
        var remote = python(3, 30);
        remote.set("account_email", ObservabilityProjection.unavailableEmail(MAPPER));
        when(python.snapshot()).thenReturn(remote);
        when(emails.observations(AccountActionEmailService.Purpose.VERIFY_EMAIL)).thenThrow(new IllegalStateException("database-secret"));
        when(emails.observations(AccountActionEmailService.Purpose.CHANGE_EMAIL)).thenReturn(Map.of("summary", Map.of("failed", 1),
                "records", List.of(record("java", "change_email", 1, NOW))));
        var result = service.snapshot(SESSION);
        assertThat(result.path("status").asText()).isEqualTo("partial");
        assertThat(result.path("requests").isObject()).isTrue();
        assertThat(result.path("account_email").path("status").asText()).isEqualTo("partial");
        assertThat(result.path("account_email").path("summary").isNull()).isTrue();
        assertThat(result.path("account_email").path("records").size()).isEqualTo(1);
        assertThat(result.toString()).doesNotContain("database-secret", "raw-recipient", "private.example", "password", "SECRET_RAW_EXCEPTION");
    }

    @Test void combinesAllThreeEmailPurposesWithPythonAndRetainsOwnerForCollidingIds() {
        for (var purpose : AccountActionEmailService.Purpose.values()) {
            when(emails.observations(purpose)).thenReturn(Map.of("summary", Map.of("failed", 2),
                    "records", List.of(record("java", purpose.name().toLowerCase(java.util.Locale.ROOT), 1, NOW))));
        }
        var remote = python(3, 30);
        ((ObjectNode) remote.get("account_email").get("summary")).put("failed", 3);
        ((ArrayNode) remote.get("account_email").get("records")).add(record("python", "verify_email", 1, NOW));
        // Client is the boundary for the Python safe projection.
        when(python.snapshot()).thenReturn(ObservabilityProjection.runtime(MAPPER, remote, true));
        var result = service.snapshot(SESSION);
        assertThat(result.path("account_email").path("summary").path("failed").asLong()).isEqualTo(9);
        assertThat(result.path("account_email").path("records").size()).isEqualTo(4);
        assertThat(result.toString()).contains("\"owner\":\"java\"", "\"owner\":\"python\"")
                .doesNotContain("raw-recipient", "private.example", "SECRET_RAW_EXCEPTION", "smtp://", "secret-token");
    }

    @Test void sortsErrorsByInstantTagsServicesAndBoundsBothRecordLists() {
        var local = runtime(1, 100);
        var remote = python(3, 30);
        for (int i = 0; i < 20; i++) {
            ((ArrayNode) local.get("api").get("recent_errors")).add(error("java-id-" + i, "2026-10-17T11:00:00+08:00"));
            ((ArrayNode) remote.get("api").get("recent_errors")).add(error("python-id-" + i, "2026-10-17T03:01:00Z"));
            ((ArrayNode) remote.get("account_email").get("records")).add(record("python", "verify_email", i + 1, NOW));
        }
        when(runtime.snapshot()).thenReturn(MAPPER.convertValue(local, Map.class));
        when(python.snapshot()).thenReturn(ObservabilityProjection.runtime(MAPPER, remote, true));
        when(emails.observations(AccountActionEmailService.Purpose.RESET_PASSWORD)).thenReturn(Map.of("summary", Map.of(),
                "records", List.of(record("java", "reset_password", 1, "2026-10-17T03:02:00Z"))));
        var result = service.snapshot(SESSION);
        var errors = result.path("requests").path("recent_errors");
        assertThat(errors.size()).isEqualTo(20);
        for (JsonNode error : errors) assertThat(error.path("service").asText()).isEqualTo("python");
        var records = result.path("account_email").path("records");
        assertThat(records.size()).isEqualTo(20);
        assertThat(records.get(0).path("owner").asText()).isEqualTo("java");
    }
}
