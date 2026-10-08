package com.jobhunting.platform.auth;

import java.util.List;
import java.util.Map;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/internal/v1/auth/{action:email-verification|password-reset}")
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class AccountActionEmailController {
    private static AccountActionEmailService.Purpose purpose(String action) {
        return "password-reset".equals(action) ? AccountActionEmailService.Purpose.RESET_PASSWORD
                : AccountActionEmailService.Purpose.VERIFY_EMAIL;
    }
    private final AccountActionEmailService service;
    private final InternalTokenVerifier tokens;
    public AccountActionEmailController(AccountActionEmailService service, InternalTokenVerifier tokens) {
        this.service = service;
        this.tokens = tokens;
    }
    @PostMapping("/request")
    public Ok request(@PathVariable String action, @Valid @RequestBody EmailRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        service.request(purpose(action), body.email(), body.source());
        return new Ok(true);
    }
    @PostMapping("/confirm")
    public RegistrationController.Result confirm(@PathVariable String action, @Valid @RequestBody TokenRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return new RegistrationController.Result(purpose(action) == AccountActionEmailService.Purpose.RESET_PASSWORD
                ? service.resetPassword(body.token(), body.new_password()) : service.verify(body.token()));
    }
    @PostMapping("/due")
    public DueResult due(@PathVariable String action, @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return new DueResult(service.due(purpose(action)));
    }
    @PostMapping("/claim")
    public ClaimResult claim(@PathVariable String action, @Valid @RequestBody IdRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return new ClaimResult(service.claim(purpose(action), body.id()));
    }
    @PostMapping("/finish")
    public Ok finish(@PathVariable String action, @Valid @RequestBody FinishRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return new Ok(service.finish(purpose(action), body.id(), body.claim_key(), body.sent(), body.error_type()));
    }
    @PostMapping("/observations")
    public Map<String, Object> observations(@PathVariable String action, @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return service.observations(purpose(action));
    }
    public record EmailRequest(@NotBlank @Email @Size(max = 254) String email, @Size(max = 64) String source) { }
    public record TokenRequest(@NotBlank @Size(max = 128) String token,
            @Size(min = 8, max = 1024) String new_password) {
        @Override public String toString() { return "TokenRequest[redacted]"; }
    }
    public record IdRequest(@Positive long id) { }
    public record FinishRequest(@Positive long id, @NotBlank @Size(max = 64) String claim_key,
            @NotNull Boolean sent, @Pattern(regexp = "[A-Za-z0-9_]{1,128}") String error_type) {
        @Override public String toString() { return "FinishRequest[redacted]"; }
    }
    public record DueResult(List<AccountActionEmailService.Due> records) { }
    public record ClaimResult(AccountActionEmailService.Claim claim) { }
    public record Ok(boolean ok) { }
}
