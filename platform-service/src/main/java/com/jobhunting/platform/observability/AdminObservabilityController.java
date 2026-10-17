package com.jobhunting.platform.observability;

import com.jobhunting.platform.billing.InternalTokenVerifier;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

@RestController
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class AdminObservabilityController {
    private final AdminObservabilityService observations;
    private final InternalTokenVerifier tokens;

    public AdminObservabilityController(AdminObservabilityService observations, InternalTokenVerifier tokens) {
        this.observations = observations;
        this.tokens = tokens;
    }

    @GetMapping("/api/admin/observability/requests")
    public ObjectNode publicSnapshot(HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        String session = null;
        if (request.getCookies() != null) {
            for (var cookie : request.getCookies()) {
                if ("job_agent_session".equals(cookie.getName())) { session = cookie.getValue(); break; }
            }
        }
        return observations.snapshot(session);
    }

    @PostMapping("/internal/v1/observability/requests")
    public ObjectNode internalSnapshot(@Valid @RequestBody SnapshotRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token,
            HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        tokens.verify(token);
        return observations.snapshot(body.session_token());
    }

    public record SnapshotRequest(@NotBlank @Size(max = 128) String session_token) {
        @Override public String toString() { return "ObservabilityRequest[redacted]"; }
    }
}
