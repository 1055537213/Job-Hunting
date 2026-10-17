package com.jobhunting.platform.observability;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

import com.jobhunting.platform.auth.AccountActionEmailService;
import com.jobhunting.platform.auth.AccountAdministrationService;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

@Service
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class AdminObservabilityService {
    private final AccountAdministrationService accounts;
    private final RuntimeRequestMetrics runtime;
    private final PythonObservabilityClient python;
    private final AccountActionEmailService emails;
    private final ObjectMapper mapper;

    public AdminObservabilityService(AccountAdministrationService accounts, RuntimeRequestMetrics runtime,
            PythonObservabilityClient python, AccountActionEmailService emails, ObjectMapper mapper) {
        this.accounts = accounts;
        this.runtime = runtime;
        this.python = python;
        this.emails = emails;
        this.mapper = mapper;
    }

    public ObjectNode snapshot(String session) {
        // Authentication failures must propagate before any service or email observations.
        accounts.authorizeAdministrator(session);
        ObjectNode javaRuntime;
        try {
            javaRuntime = ObservabilityProjection.runtime(mapper, mapper.valueToTree(runtime.snapshot()), false);
        } catch (IllegalArgumentException invalid) {
            javaRuntime = ObservabilityProjection.unavailable(mapper);
        }
        var pythonSnapshot = python.snapshot();
        var pythonRuntime = pythonSnapshot.deepCopy();
        pythonRuntime.remove("account_email");
        boolean metricsComplete = javaRuntime.path("api").isObject() && pythonRuntime.path("api").isObject();
        var email = combinedEmail(pythonSnapshot.get("account_email"));
        var result = mapper.createObjectNode();
        result.put("status", metricsComplete && "available".equals(email.path("status").asText()) ? "available" : "partial");
        if (metricsComplete) result.set("requests", combinedRequests(javaRuntime.get("api"), pythonRuntime.get("api")));
        else result.putNull("requests");
        var services = result.putObject("services");
        services.set("java", javaRuntime);
        services.set("python", pythonRuntime);
        result.set("account_email", email);
        return result;
    }

    private ObjectNode combinedEmail(JsonNode pythonEmail) {
        var summary = mapper.createObjectNode();
        for (String state : List.of("pending", "sending", "retrying", "sent", "failed", "cancelled")) summary.put(state, 0L);
        var records = new ArrayList<JsonNode>();
        boolean complete = true;
        for (var purpose : AccountActionEmailService.Purpose.values()) {
            try {
                var projected = ObservabilityProjection.email(mapper, mapper.valueToTree(emails.observations(purpose)), "java", false);
                mergeCounts(summary, projected.get("summary"), "");
                projected.get("records").forEach(records::add);
            } catch (RuntimeException unavailable) {
                // A failed purpose query makes totals unknown; other safe records remain useful.
                complete = false;
            }
        }
        if (pythonEmail != null && "available".equals(pythonEmail.path("status").asText())) {
            mergeCounts(summary, pythonEmail.get("summary"), "");
            pythonEmail.get("records").forEach(records::add);
        } else complete = false;
        var result = mapper.createObjectNode();
        result.put("status", complete ? "available" : "partial");
        if (complete) result.set("summary", summary); else result.putNull("summary");
        records.sort(Comparator.<JsonNode, java.time.Instant>comparing(record -> ObservabilityProjection.instant(record.get("created_at")))
                .reversed().thenComparing(record -> record.path("owner").asText())
                .thenComparing(Comparator.comparingLong((JsonNode record) -> record.path("id").asLong()).reversed()));
        var top = result.putArray("records");
        records.stream().limit(20).forEach(top::add);
        return result;
    }

    private ObjectNode combinedRequests(JsonNode javaApi, JsonNode pythonApi) {
        var result = mapper.createObjectNode();
        var started = ObservabilityProjection.instant(javaApi.get("started_at"));
        var pythonStarted = ObservabilityProjection.instant(pythonApi.get("started_at"));
        result.put("started_at", (started.isBefore(pythonStarted) ? started : pythonStarted).toString());
        for (String key : ObservabilityProjection.COUNTS) {
            result.put(key, javaApi.get(key).bigIntegerValue().add(pythonApi.get(key).bigIntegerValue()));
        }
        BigDecimal duration = javaApi.get("total_duration_ms").decimalValue().add(pythonApi.get("total_duration_ms").decimalValue());
        BigDecimal count = result.get("total_requests").decimalValue();
        result.put("total_duration_ms", duration);
        result.put("average_duration_ms", count.signum() == 0 ? BigDecimal.ZERO : duration.divide(count, 2, RoundingMode.HALF_UP));
        result.put("max_duration_ms", javaApi.get("max_duration_ms").decimalValue().max(pythonApi.get("max_duration_ms").decimalValue()));
        for (String key : ObservabilityProjection.MAPS) {
            var merged = result.putObject(key);
            boolean endpoints = "endpoint_counts".equals(key);
            mergeCounts(merged, javaApi.get(key), endpoints ? "java:" : "");
            mergeCounts(merged, pythonApi.get(key), endpoints ? "python:" : "");
        }
        var errors = new ArrayList<JsonNode>();
        taggedErrors(errors, javaApi, "java");
        taggedErrors(errors, pythonApi, "python");
        errors.sort(Comparator.<JsonNode, java.time.Instant>comparing(error -> ObservabilityProjection.instant(error.get("created_at"))).reversed());
        var recent = result.putArray("recent_errors");
        errors.stream().limit(20).forEach(recent::add);
        return result;
    }

    private static void taggedErrors(List<JsonNode> errors, JsonNode snapshot, String service) {
        for (JsonNode error : snapshot.get("recent_errors")) {
            var tagged = (ObjectNode) error.deepCopy();
            tagged.put("service", service);
            errors.add(tagged);
        }
    }

    private static void mergeCounts(ObjectNode target, JsonNode source, String prefix) {
        for (String key : source.propertyNames()) {
            String name = prefix + key;
            var previous = target.get(name);
            target.put(name, source.get(key).bigIntegerValue().add(previous == null ? java.math.BigInteger.ZERO : previous.bigIntegerValue()));
        }
    }
}
