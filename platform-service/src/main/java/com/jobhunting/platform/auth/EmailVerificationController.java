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
@RequestMapping("/internal/v1/auth/email-verification")
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class EmailVerificationController {
    private final EmailVerificationService service;
    private final InternalTokenVerifier tokens;
    public EmailVerificationController(EmailVerificationService service, InternalTokenVerifier tokens) {
        this.service = service;
        this.tokens = tokens;
    }
    @PostMapping("/request")
    public Ok request(@Valid @RequestBody EmailRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        service.request(body.email(), body.source());
        return new Ok(true);
    }
    @PostMapping("/confirm")
    public RegistrationController.Result confirm(@Valid @RequestBody TokenRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return new RegistrationController.Result(service.verify(body.token()));
    }
    @PostMapping("/due")
    public DueResult due(@RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return new DueResult(service.due());
    }
    @PostMapping("/claim")
    public ClaimResult claim(@Valid @RequestBody IdRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return new ClaimResult(service.claim(body.id()));
    }
    @PostMapping("/finish")
    public Ok finish(@Valid @RequestBody FinishRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return new Ok(service.finish(body.id(), body.claim_key(), body.sent(), body.error_type()));
    }
    @PostMapping("/observations")
    public Map<String, Object> observations(@RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return service.observations();
    }
    public record EmailRequest(@NotBlank @Email @Size(max = 254) String email, @Size(max = 64) String source) { }
    public record TokenRequest(@NotBlank @Size(max = 128) String token) {
        @Override public String toString() { return "TokenRequest[redacted]"; }
    }
    public record IdRequest(@Positive long id) { }
    public record FinishRequest(@Positive long id, @NotBlank @Size(max = 64) String claim_key,
            @NotNull Boolean sent, @Pattern(regexp = "[A-Za-z0-9_]{1,128}") String error_type) {
        @Override public String toString() { return "FinishRequest[redacted]"; }
    }
    public record DueResult(List<EmailVerificationService.Due> records) { }
    public record ClaimResult(EmailVerificationService.Claim claim) { }
    public record Ok(boolean ok) { }
}
