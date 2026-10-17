package com.jobhunting.platform.tasks;

import java.util.Map;
import com.jobhunting.platform.auth.AuthException;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import jakarta.validation.constraints.*;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;

@RestController
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class TaskReadController {
    private final TaskReadService service;
    private final InternalTokenVerifier tokens;

    public TaskReadController(TaskReadService service, InternalTokenVerifier tokens) {
        this.service = service;
        this.tokens = tokens;
    }

    @GetMapping("/api/admin/tasks")
    public TaskQueryService.TaskPage admin(HttpServletRequest request,
            @RequestParam(required = false) @Min(1) Long account_id,
            @RequestParam(required = false) @Size(min = 1, max = 80) String task_type,
            @RequestParam(required = false) @Size(min = 1, max = 20) String status,
            @RequestParam(defaultValue = "100") @Min(1) @Max(100) int limit,
            @RequestParam(defaultValue = "0") @Min(0) long offset) {
        return service.adminList(session(request), account_id, task_type, status, limit, offset);
    }

    @GetMapping("/api/tasks/{taskKey}")
    public Map<String, Object> personal(HttpServletRequest request, @PathVariable @Size(min = 1, max = 64) String taskKey) {
        return service.personal(session(request), taskKey);
    }

    @PostMapping("/internal/v1/tasks/admin/query")
    public TaskQueryService.TaskPage internalAdmin(@Valid @RequestBody PageRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return service.adminList(body.session_token(), body.account_id(), body.task_type(), body.status(), body.limit(), body.offset());
    }

    @PostMapping("/internal/v1/tasks/me/query")
    public Map<String, Object> internalPersonal(@Valid @RequestBody TaskRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return service.personal(body.session_token(), body.task_key());
    }

    private static String session(HttpServletRequest request) {
        if (request.getCookies() != null) for (var cookie : request.getCookies()) {
            if ("job_agent_session".equals(cookie.getName()) && !cookie.getValue().isBlank() && cookie.getValue().length() <= 128) return cookie.getValue();
        }
        throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
    }

    public record PageRequest(@NotBlank @Size(max = 128) String session_token, @Min(1) Long account_id,
            @Size(min = 1, max = 80) String task_type, @Size(min = 1, max = 20) String status,
            @NotNull @Min(1) @Max(100) Integer limit, @NotNull @Min(0) Long offset) {
        @Override public String toString() { return "AdminTaskReadRequest[redacted]"; }
    }
    public record TaskRequest(@NotBlank @Size(max = 128) String session_token, @NotBlank @Size(max = 64) String task_key) {
        @Override public String toString() { return "PersonalTaskReadRequest[redacted]"; }
    }
}
