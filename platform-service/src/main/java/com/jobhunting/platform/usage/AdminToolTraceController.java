package com.jobhunting.platform.usage;

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
public class AdminToolTraceController {
    private final AdminToolTraceService service;
    private final InternalTokenVerifier tokens;

    public AdminToolTraceController(AdminToolTraceService service, InternalTokenVerifier tokens) {
        this.service = service;
        this.tokens = tokens;
    }

    @GetMapping("/api/admin/tools/traces")
    public AdminToolTraceQueryService.TracePage list(HttpServletRequest request, @RequestParam(required = false) Long account_id,
            @RequestParam(defaultValue = "100") @Min(1) @Max(100) int limit,
            @RequestParam(defaultValue = "0") @Min(0) long offset) {
        return service.list(session(request), account_id, limit, offset);
    }

    @GetMapping("/api/admin/tools/traces/{rootRequestId}")
    public Map<String, Object> detail(HttpServletRequest request, @PathVariable @Size(min = 1, max = 128) String rootRequestId) {
        return service.detail(session(request), rootRequestId);
    }

    @PostMapping("/internal/v1/admin/tools/traces")
    public AdminToolTraceQueryService.TracePage internalList(@Valid @RequestBody PageRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return service.list(body.session_token(), body.account_id(), body.limit(), body.offset());
    }

    @PostMapping("/internal/v1/admin/tools/traces/detail")
    public Map<String, Object> internalDetail(@Valid @RequestBody DetailRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return service.detail(body.session_token(), body.root_request_id());
    }

    private static String session(HttpServletRequest request) {
        if (request.getCookies() != null) for (var cookie : request.getCookies()) {
            if ("job_agent_session".equals(cookie.getName()) && !cookie.getValue().isBlank() && cookie.getValue().length() <= 128) return cookie.getValue();
        }
        throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
    }

    public record PageRequest(@NotBlank @Size(max = 128) String session_token, Long account_id,
            @NotNull @Min(1) @Max(100) Integer limit, @NotNull @Min(0) Long offset) {
        @Override public String toString() { return "ToolTracePageRequest[redacted]"; }
    }
    public record DetailRequest(@NotBlank @Size(max = 128) String session_token, @NotBlank @Size(max = 128) String root_request_id) {
        @Override public String toString() { return "ToolTraceDetailRequest[redacted]"; }
    }
}
