package com.jobhunting.platform.observability;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.HashSet;
import java.util.Set;
import java.util.function.Predicate;

import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** Strict, low-sensitivity projections at the service boundary. */
final class ObservabilityProjection {
    static final Set<String> COUNTS = Set.of("in_flight_requests", "total_requests", "error_requests",
            "rate_limited_requests", "rate_limit_backend_errors", "csrf_rejected_requests", "exception_requests");
    static final Set<String> MAPS = Set.of("status_counts", "method_counts", "endpoint_counts", "outcome_counts");
    private static final Set<String> METHODS = Set.of("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "OTHER");
    private static final Set<String> OUTCOMES = Set.of("handled", "invalid_path", "invalid_address", "rate_limited",
            "unauthenticated", "forbidden", "csrf_rejected", "auth_rejected", "rate_limit_backend_unavailable", "exception",
            "protection_unavailable");
    private static final Set<String> EMAIL_STATUSES = Set.of("pending", "sending", "retrying", "sent", "failed", "cancelled");
    private static final Set<String> PURPOSES = Set.of("verify_email", "reset_password", "change_email");
    private static final Set<String> ERROR_FIELDS = Set.of("request_id", "method", "endpoint", "status_code", "outcome", "duration_ms", "created_at");
    private static final Set<String> EMAIL_FIELDS = Set.of("owner", "id", "account_id", "purpose", "recipient_email",
            "status", "attempt_count", "max_attempts", "last_error_type", "last_error_summary", "created_at", "updated_at");

    private ObservabilityProjection() { }

    static ObjectNode unavailable(ObjectMapper mapper) {
        var result = mapper.createObjectNode();
        result.put("status", "unavailable");
        result.putNull("api");
        result.putNull("internal");
        result.putNull("observed_at");
        return result;
    }

    static ObjectNode unavailableEmail(ObjectMapper mapper) {
        var result = mapper.createObjectNode();
        result.put("status", "unavailable");
        result.putNull("summary");
        result.putArray("records");
        return result;
    }

    static ObjectNode runtime(ObjectMapper mapper, JsonNode value, boolean python) {
        require(fields(value, python ? Set.of("status", "api", "internal", "observed_at", "account_email")
                : Set.of("status", "api", "internal", "observed_at")));
        require("available".equals(value.path("status").asText()));
        var result = mapper.createObjectNode();
        result.put("status", "available");
        result.set("api", metrics(mapper, value.get("api"), "/api/"));
        result.set("internal", metrics(mapper, value.get("internal"), "/internal/"));
        result.put("observed_at", timestamp(value.get("observed_at")));
        if (python) {
            try {
                result.set("account_email", email(mapper, value.get("account_email"), "python", true));
            } catch (IllegalArgumentException invalid) {
                result.set("account_email", unavailableEmail(mapper));
            }
        }
        return result;
    }

    static ObjectNode metrics(ObjectMapper mapper, JsonNode value, String scope) {
        var expected = new HashSet<>(COUNTS);
        expected.addAll(MAPS);
        expected.addAll(Set.of("started_at", "total_duration_ms", "average_duration_ms", "max_duration_ms", "recent_errors"));
        require(fields(value, expected));
        var result = mapper.createObjectNode();
        result.put("started_at", timestamp(value.get("started_at")));
        for (String key : COUNTS) {
            require(count(value.get(key)));
            result.set(key, value.get(key));
        }
        for (String key : Set.of("total_duration_ms", "average_duration_ms", "max_duration_ms")) {
            require(number(value.get(key)));
            result.set(key, value.get(key));
        }
        for (String key : MAPS) {
            Predicate<String> safeKey = switch (key) {
                case "status_counts" -> name -> name.matches("[1-5]xx");
                case "method_counts" -> METHODS::contains;
                case "outcome_counts" -> OUTCOMES::contains;
                default -> name -> endpoint(name, scope);
            };
            result.set(key, counts(mapper, value.get(key), safeKey, 130));
        }
        JsonNode errors = value.get("recent_errors");
        require(errors != null && errors.isArray() && errors.size() <= 20);
        var projected = result.putArray("recent_errors");
        for (JsonNode error : errors) {
            require(fields(error, ERROR_FIELDS) && text(error.get("request_id"), 128)
                    && error.get("request_id").asText().matches("[A-Za-z0-9._-]{8,128}")
                    && METHODS.contains(error.path("method").asText())
                    && endpoint(error.path("endpoint").asText(), scope)
                    && count(error.get("status_code")) && error.get("status_code").asLong() >= 100
                    && error.get("status_code").asLong() <= 599
                    && OUTCOMES.contains(error.path("outcome").asText()) && number(error.get("duration_ms")));
            var copy = (ObjectNode) error.deepCopy();
            copy.put("created_at", timestamp(error.get("created_at")));
            projected.add(copy);
        }
        return result;
    }

    static ObjectNode email(ObjectMapper mapper, JsonNode value, String owner, boolean hasStatus) {
        require(fields(value, hasStatus ? Set.of("status", "summary", "records") : Set.of("summary", "records")));
        if (hasStatus && "unavailable".equals(value.path("status").asText())) {
            require(value.path("summary").isNull() && value.path("records").isArray() && value.path("records").isEmpty());
            return unavailableEmail(mapper);
        }
        require(!hasStatus || "available".equals(value.path("status").asText()));
        var result = mapper.createObjectNode();
        result.put("status", "available");
        result.set("summary", counts(mapper, value.get("summary"), EMAIL_STATUSES::contains, 10));
        JsonNode records = value.get("records");
        require(records != null && records.isArray() && records.size() <= 20);
        var projected = result.putArray("records");
        for (JsonNode record : records) {
            require(fields(record, EMAIL_FIELDS) && owner.equals(record.path("owner").asText())
                    && count(record.get("id")) && record.get("id").asLong() > 0
                    && count(record.get("account_id")) && record.get("account_id").asLong() > 0
                    && PURPOSES.contains(record.path("purpose").asText())
                    && EMAIL_STATUSES.contains(record.path("status").asText())
                    && count(record.get("attempt_count")) && count(record.get("max_attempts"))
                    && record.get("max_attempts").asLong() > 0 && text(record.get("recipient_email"), 320)
                    && nullableText(record.get("last_error_type")) && nullableText(record.get("last_error_summary")));
            var copy = (ObjectNode) record.deepCopy();
            copy.put("recipient_email", mask(record.get("recipient_email").asText()));
            copy.put("created_at", timestamp(record.get("created_at")));
            copy.put("updated_at", timestamp(record.get("updated_at")));
            boolean error = !record.get("last_error_type").isNull();
            if (error) {
                copy.put("last_error_type", "DeliveryError");
                copy.put("last_error_summary", "Email delivery did not complete.");
            } else {
                copy.putNull("last_error_type");
                copy.putNull("last_error_summary");
            }
            projected.add(copy);
        }
        return result;
    }

    private static String mask(String value) {
        // Both local and domain parts may contain credentials in malformed legacy records.
        return value.contains("@") ? "***@***" : "***";
    }

    static ObjectNode counts(ObjectMapper mapper, JsonNode value, Predicate<String> safeKey, int limit) {
        require(value != null && value.isObject() && value.size() <= limit);
        var result = mapper.createObjectNode();
        for (String key : value.propertyNames()) {
            require(safeKey.test(key) && count(value.get(key)));
            result.set(key, value.get(key));
        }
        return result;
    }

    static Instant instant(JsonNode value) {
        require(text(value, 64));
        try {
            return OffsetDateTime.parse(value.asText()).toInstant();
        } catch (java.time.DateTimeException invalid) {
            throw new IllegalArgumentException("Invalid observability timestamp");
        }
    }

    private static String timestamp(JsonNode value) { return instant(value).toString(); }

    private static boolean endpoint(String value, String scope) {
        return Set.of("unmatched", "other").contains(value)
                || (value.startsWith(scope) && value.length() <= 256 && value.matches("/[A-Za-z0-9_/{}/.:-]+"));
    }

    private static boolean fields(JsonNode value, Set<String> expected) {
        return value != null && value.isObject() && value.size() == expected.size()
                && new HashSet<>(value.propertyNames()).equals(expected);
    }

    private static boolean count(JsonNode value) {
        return value != null && value.isIntegralNumber() && value.canConvertToLong() && value.asLong() >= 0;
    }

    private static boolean number(JsonNode value) {
        return value != null && value.isNumber() && Double.isFinite(value.asDouble()) && value.asDouble() >= 0;
    }

    private static boolean text(JsonNode value, int max) {
        return value != null && value.isString() && !value.asText().isBlank() && value.asText().length() <= max;
    }

    private static boolean nullableText(JsonNode value) { return value != null && (value.isNull() || value.isString()); }
    private static void require(boolean valid) { if (!valid) throw new IllegalArgumentException("Invalid observability projection"); }
}
