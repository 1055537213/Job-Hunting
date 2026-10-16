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
public class AdminUsageController {
    private final AdminUsageService service;
    private final InternalTokenVerifier tokens;

    public AdminUsageController(AdminUsageService service, InternalTokenVerifier tokens) {
        this.service = service;
        this.tokens = tokens;
    }

    @GetMapping("/api/admin/usage/summary")
    public Map<String, Object> summary(HttpServletRequest request) { return service.summary(session(request)); }

    @GetMapping("/api/admin/usage/events")
    public AdminUsageQueryService.UsagePage events(HttpServletRequest request,
            @RequestParam(required = false) Long account_id, @RequestParam(required = false) Long candidate_id,
            @RequestParam(required = false) String session_id,
            @RequestParam(defaultValue = "100") @Min(1) @Max(100) int limit,
            @RequestParam(defaultValue = "0") @Min(0) long offset) {
        return service.events(session(request), account_id, candidate_id, session_id, limit, offset);
    }

    @PostMapping("/internal/v1/admin/usage/summary")
    public Map<String, Object> internalSummary(@Valid @RequestBody SessionRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return service.summary(body.session_token());
    }

    @PostMapping("/internal/v1/admin/usage/events")
    public AdminUsageQueryService.UsagePage internalEvents(@Valid @RequestBody PageRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return service.events(body.session_token(), body.account_id(), body.candidate_id(), body.session_id(), body.limit(), body.offset());
    }

    private static String session(HttpServletRequest request) {
        if (request.getCookies() != null) for (var cookie : request.getCookies()) {
            if ("job_agent_session".equals(cookie.getName()) && !cookie.getValue().isBlank() && cookie.getValue().length() <= 128) return cookie.getValue();
        }
        throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
    }

    public record SessionRequest(@NotBlank @Size(max = 128) String session_token) {
        @Override public String toString() { return "UsageSessionRequest[redacted]"; }
    }
    public record PageRequest(@NotBlank @Size(max = 128) String session_token, Long account_id, Long candidate_id, String session_id,
            @NotNull @Min(1) @Max(100) Integer limit, @NotNull @Min(0) Long offset) {
        @Override public String toString() { return "UsagePageRequest[redacted]"; }
    }
}
