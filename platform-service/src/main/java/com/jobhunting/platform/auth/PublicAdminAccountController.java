package com.jobhunting.platform.auth;

import java.util.Map;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import jakarta.validation.constraints.*;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;
import com.jobhunting.platform.billing.InternalTokenVerifier;

@RestController
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class PublicAdminAccountController {
    private final AccountAdministrationService accounts;
    private final AdminAuditService audit;
    private final InternalTokenVerifier tokens;

    public PublicAdminAccountController(AccountAdministrationService accounts, AdminAuditService audit, InternalTokenVerifier tokens) {
        this.accounts = accounts;
        this.audit = audit;
        this.tokens = tokens;
    }

    @GetMapping("/api/admin/accounts")
    public Map<String, Object> list(HttpServletRequest request) {
        return Map.of("accounts", accounts.list(session(request)).accounts().stream()
                .map(PublicAuthController::publicAccount).toList());
    }

    @PatchMapping("/api/admin/accounts/{accountId}/status")
    public Map<String, Object> status(HttpServletRequest request, @PathVariable @Positive long accountId,
            @Valid @RequestBody StatusRequest body) {
        return Map.of("account", PublicAuthController.publicAccount(accounts.updateStatus(session(request), accountId,
                body.status(), request.getHeader("X-Request-ID")).account()));
    }

    @GetMapping("/api/admin/audit/events")
    public AdminAuditService.AuditPage audit(HttpServletRequest request,
            @RequestParam(defaultValue = "50") @Min(1) @Max(200) int limit,
            @RequestParam(defaultValue = "0") @Min(0) long offset) {
        return audit.page(session(request), limit, offset);
    }

    @PostMapping("/internal/v1/auth/admin/audit/events")
    public AdminAuditService.AuditPage internalAudit(@Valid @RequestBody AuditRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return audit.page(body.session_token(), body.limit(), body.offset());
    }

    private static String session(HttpServletRequest request) {
        String token = PublicAuthController.cookie(request, PublicAuthController.SESSION_COOKIE);
        if (token == null || token.isBlank() || token.length() > 128) {
            throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
        }
        return token;
    }

    public record StatusRequest(@NotBlank @Size(max = 32) String status) { }
    public record AuditRequest(@NotBlank @Size(max = 128) String session_token,
            @NotNull @Min(1) @Max(200) Integer limit, @NotNull @Min(0) Long offset) {
        @Override public String toString() { return "AdminAuditRequest[redacted]"; }
    }
}
