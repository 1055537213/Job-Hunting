package com.jobhunting.platform.auth;

import com.jobhunting.platform.billing.InternalTokenVerifier;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/internal/v1/auth/sessions")
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class SessionController {
    private final SessionService sessions;
    private final InternalTokenVerifier tokens;
    public SessionController(SessionService sessions, InternalTokenVerifier tokens) {
        this.sessions = sessions;
        this.tokens = tokens;
    }
    @PostMapping("/login")
    public SessionService.Login login(@Valid @RequestBody LoginRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return sessions.login(body.email(), body.password(), body.email_verification_required(), body.user_agent(), body.ip_address());
    }
    @PostMapping("/resolve")
    public Resolved resolve(@Valid @RequestBody TokenRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return new Resolved(sessions.resolve(body.session_token()));
    }
    @PostMapping("/logout")
    public Ok logout(@Valid @RequestBody TokenRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        sessions.logout(body.session_token());
        return new Ok(true);
    }
    @PostMapping("/logout-all")
    public Revoked logoutAll(@Valid @RequestBody TokenRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token,
            @RequestHeader(name = "X-Trace-Id", defaultValue = "") String traceId) {
        tokens.verify(token);
        return new Revoked(true, sessions.logoutAll(body.session_token(), traceId.substring(0, Math.min(128, traceId.length()))));
    }
    @PostMapping("/change-password")
    public Ok changePassword(@Valid @RequestBody PasswordRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        sessions.changePassword(body.session_token(), body.current_password(), body.new_password());
        return new Ok(true);
    }
    public record LoginRequest(@NotBlank @Size(max = 254) String email,
            @NotBlank @Size(max = 1024) String password, boolean email_verification_required,
            @Size(max = 1024) String user_agent, @Size(max = 64) String ip_address) {
        @Override public String toString() { return "LoginRequest[redacted]"; }
    }
    public record TokenRequest(@NotBlank @Size(max = 128) String session_token) {
        @Override public String toString() { return "TokenRequest[redacted]"; }
    }
    public record PasswordRequest(@NotBlank @Size(max = 128) String session_token,
            @NotBlank @Size(max = 1024) String current_password, @NotBlank @Size(min = 8, max = 1024) String new_password) {
        @Override public String toString() { return "PasswordRequest[redacted]"; }
    }
    public record Resolved(Long account_id) { }
    public record Ok(boolean ok) { }
    public record Revoked(boolean ok, int revoked_sessions) { }
}
