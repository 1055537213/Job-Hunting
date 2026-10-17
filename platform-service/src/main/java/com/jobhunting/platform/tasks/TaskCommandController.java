package com.jobhunting.platform.tasks;

import com.jobhunting.platform.billing.InternalTokenVerifier;
import com.jobhunting.platform.security.WebSecurityFilter;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;

@RestController
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class TaskCommandController {
    private final TaskCommandService commands;
    private final InternalTokenVerifier tokens;

    public TaskCommandController(TaskCommandService commands, InternalTokenVerifier tokens) {
        this.commands = commands;
        this.tokens = tokens;
    }

    @PostMapping("/api/admin/tasks/probe")
    public JsonNode probe(HttpServletRequest request,
            @RequestHeader(name = "Idempotency-Key", required = false) String key) {
        return commands.submit(session(request), "probe", null, key, requestId(request));
    }

    @PostMapping("/api/admin/tasks/{taskKey}/retry")
    public JsonNode retry(HttpServletRequest request, @PathVariable String taskKey,
            @RequestHeader(name = "Idempotency-Key", required = false) String key) {
        return commands.submit(session(request), "retry", taskKey, key, requestId(request));
    }

    @PostMapping("/internal/v1/tasks/admin/command")
    public JsonNode internal(@Valid @RequestBody CommandRequest body, HttpServletRequest request,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return commands.submit(body.session_token(), body.operation(), body.task_key(), body.idempotency_key(), requestId(request));
    }

    private static String session(HttpServletRequest request) {
        if (request.getCookies() != null) {
            for (var cookie : request.getCookies()) {
                if ("job_agent_session".equals(cookie.getName())) return cookie.getValue();
            }
        }
        return null;
    }

    private static String requestId(HttpServletRequest request) {
        return WebSecurityFilter.requestId(request.getHeader("X-Request-ID"), request.getHeader("X-Trace-Id"));
    }

    public record CommandRequest(@NotBlank @Size(max = 128) String session_token,
            @NotBlank @Size(max = 16) String operation, @Size(max = 64) String task_key,
            @Size(max = 128) String idempotency_key) {
        @Override public String toString() { return "TaskCommandRequest[redacted]"; }
    }
}
