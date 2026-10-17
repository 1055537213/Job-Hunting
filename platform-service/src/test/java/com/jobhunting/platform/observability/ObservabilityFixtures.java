package com.jobhunting.platform.observability;

import java.util.Map;

import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

final class ObservabilityFixtures {
    static final ObjectMapper MAPPER = new ObjectMapper();
    static final String TOKEN = "test-internal-observability-secret-12345";
    static final String SESSION = "real-admin-session";
    static final String NOW = "2026-10-17T03:00:00Z";

    static ObjectNode metrics(String scope, long requests, double duration) {
        var value = MAPPER.createObjectNode();
        value.put("started_at", NOW);
        for (String key : ObservabilityProjection.COUNTS) value.put(key, 0L);
        value.put("total_requests", requests);
        value.put("total_duration_ms", duration);
        value.put("average_duration_ms", requests == 0 ? 0 : duration / requests);
        value.put("max_duration_ms", requests == 0 ? 0 : duration / requests);
        for (String key : ObservabilityProjection.MAPS) value.putObject(key);
        if (requests > 0) {
            ((ObjectNode) value.get("status_counts")).put("2xx", requests);
            ((ObjectNode) value.get("method_counts")).put("GET", requests);
            ((ObjectNode) value.get("endpoint_counts")).put(scope + "health", requests);
            ((ObjectNode) value.get("outcome_counts")).put("handled", requests);
        }
        value.putArray("recent_errors");
        return value;
    }

    static ObjectNode runtime(long requests, double duration) {
        var value = MAPPER.createObjectNode();
        value.put("status", "available");
        value.set("api", metrics("/api/", requests, duration));
        value.set("internal", metrics("/internal/", 100, 9000));
        value.put("observed_at", NOW);
        return value;
    }

    static ObjectNode python(long requests, double duration) {
        var value = runtime(requests, duration);
        var email = value.putObject("account_email");
        email.put("status", "available");
        email.set("summary", MAPPER.valueToTree(Map.of("pending", 0, "sending", 0, "retrying", 0, "sent", 0, "failed", 0, "cancelled", 0)));
        email.putArray("records");
        return value;
    }

    static ObjectNode record(String owner, String purpose, long id, String time) {
        var value = MAPPER.createObjectNode();
        value.put("owner", owner);
        value.put("id", id);
        value.put("account_id", 7);
        value.put("purpose", purpose);
        value.put("recipient_email", "raw-recipient@private.example");
        value.put("status", "failed");
        value.put("attempt_count", 1);
        value.put("max_attempts", 5);
        value.put("last_error_type", "SECRET_RAW_EXCEPTION");
        value.put("last_error_summary", "smtp://user:password@host secret-token");
        value.put("created_at", time);
        value.put("updated_at", time);
        return value;
    }

    static ObjectNode error(String id, String time) {
        var value = MAPPER.createObjectNode();
        value.put("request_id", id);
        value.put("method", "GET");
        value.put("endpoint", "/api/health");
        value.put("status_code", 500);
        value.put("outcome", "exception");
        value.put("duration_ms", 3);
        value.put("created_at", time);
        return value;
    }

    private ObservabilityFixtures() { }
}
