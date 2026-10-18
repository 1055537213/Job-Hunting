package com.jobhunting.platform.auth;

import java.util.Map;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseCookie;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

@RestController
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class AccountDeletionController {
    private final AccountAdministrationService accounts;
    private final AccountDeletionRequests requests;
    private final InternalTokenVerifier tokens;
    private final boolean secure;

    public AccountDeletionController(AccountAdministrationService accounts, AccountDeletionRequests requests, InternalTokenVerifier tokens,
            @Value("${JOB_AGENT_COOKIE_SECURE:false}") boolean secure) {
        this.accounts = accounts; this.requests = requests; this.tokens = tokens; this.secure = secure;
    }

    @PostMapping("/api/account/delete")
    @ResponseStatus(HttpStatus.ACCEPTED)
    public Map<String, Object> delete(@Valid @RequestBody DeleteRequest body, HttpServletRequest request, HttpServletResponse response) {
        var result = admit(PublicAuthController.cookie(request, PublicAuthController.SESSION_COOKIE), body.current_password(),
                body.confirmation(), request.getHeader("X-Request-ID"));
        response.setHeader("Cache-Control", "no-store");
        for (String name : new String[] {PublicAuthController.SESSION_COOKIE, PublicAuthController.CSRF_COOKIE}) {
            response.addHeader("Set-Cookie", ResponseCookie.from(name, "").path("/").sameSite("Lax")
                    .httpOnly(name.equals(PublicAuthController.SESSION_COOKIE)).secure(secure).maxAge(0).build().toString());
        }
        return result;
    }

    @PostMapping("/internal/v1/accounts/deletions/request")
    @ResponseStatus(HttpStatus.ACCEPTED)
    public Map<String, Object> internal(@Valid @RequestBody InternalDeleteRequest body,
            @RequestHeader(name="X-Internal-Service-Token", required=false) String token,
            @RequestHeader(name="X-Trace-Id", defaultValue="") String traceId, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        tokens.verify(token);
        return admit(body.session_token(), body.current_password(), body.confirmation(), traceId);
    }

    @PostMapping("/internal/v1/accounts/deletions/due")
    public Map<String, Object> due(@Valid @RequestBody DueRequest body,
            @RequestHeader(name="X-Internal-Service-Token", required=false) String token, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        tokens.verify(token);
        return Map.of("requests", requests.due(body.limit()));
    }

    private Map<String, Object> admit(String session, String password, String confirmation, String requestId) {
        if (session == null || session.isBlank() || session.length() > 128) {
            throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
        }
        if (!"注销账号".equals(confirmation)) {
            throw new AuthException("INVALID_CONFIRMATION", "请输入“注销账号”完成确认。", HttpStatus.BAD_REQUEST);
        }
        var account = accounts.prepareDeletion(session, password, requestId).account();
        return Map.of("ok", true, "status", "accepted", "task_key", requests.forAccount(account.account_id()).task_key());
    }

    public record DeleteRequest(@NotBlank @Size(max=1024) String current_password, @NotBlank @Size(max=32) String confirmation) {
        @Override public String toString() { return "DeleteRequest[redacted]"; }
    }
    public record InternalDeleteRequest(@NotBlank @Size(max=128) String session_token,
            @NotBlank @Size(max=1024) String current_password, @NotBlank @Size(max=32) String confirmation) {
        @Override public String toString() { return "InternalDeleteRequest[redacted]"; }
    }
    public record DueRequest(@Min(1) @Max(20) int limit) { }
}
