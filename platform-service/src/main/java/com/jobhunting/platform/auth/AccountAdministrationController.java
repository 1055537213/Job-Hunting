package com.jobhunting.platform.auth;

import com.jobhunting.platform.billing.InternalTokenVerifier;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/internal/v1/auth/accounts")
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class AccountAdministrationController {
    private final AccountAdministrationService accounts;
    private final InternalTokenVerifier tokens;

    public AccountAdministrationController(AccountAdministrationService accounts, InternalTokenVerifier tokens) {
        this.accounts = accounts;
        this.tokens = tokens;
    }

    @PostMapping("/list")
    public AccountAdministrationService.AccountList list(
            @Valid @RequestBody SessionRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return accounts.list(body.session_token());
    }

    @PostMapping("/me")
    public AccountAdministrationService.AccountResponse me(
            @Valid @RequestBody SessionRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return accounts.current(body.session_token());
    }

    @PostMapping("/status")
    public AccountAdministrationService.AccountResponse status(
            @Valid @RequestBody StatusRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token,
            @RequestHeader(name = "X-Trace-Id", defaultValue = "") String traceId) {
        tokens.verify(token);
        return accounts.updateStatus(body.session_token(), body.account_id(), body.status(), traceId);
    }

    @PostMapping("/bootstrap")
    public AccountAdministrationService.BootstrapResult bootstrap(
            @Valid @RequestBody BootstrapRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return accounts.bootstrap(body.email(), body.password(), body.display_name());
    }

    @PostMapping("/delete-admission")
    public AccountAdministrationService.AccountResponse deleteAdmission(
            @Valid @RequestBody DeleteAdmissionRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token,
            @RequestHeader(name = "X-Trace-Id", defaultValue = "") String traceId) {
        tokens.verify(token);
        return accounts.prepareDeletion(body.session_token(), body.current_password(), traceId);
    }

    public record SessionRequest(@NotBlank @Size(max = 128) String session_token) {
        @Override public String toString() { return "SessionRequest[redacted]"; }
    }

    public record StatusRequest(
            @NotBlank @Size(max = 128) String session_token,
            @NotNull @Positive Long account_id,
            @NotBlank @Size(max = 32) String status) {
        @Override public String toString() { return "StatusRequest[redacted]"; }
    }

    public record BootstrapRequest(
            @NotBlank @Email @Size(max = 254) String email,
            @NotBlank @Size(min = 8, max = 1024) String password,
            @Size(max = 128) String display_name) {
        @Override public String toString() { return "BootstrapRequest[redacted]"; }
    }

    public record DeleteAdmissionRequest(
            @NotBlank @Size(max = 128) String session_token,
            @NotBlank @Size(max = 1024) String current_password) {
        @Override public String toString() { return "DeleteAdmissionRequest[redacted]"; }
    }
}
