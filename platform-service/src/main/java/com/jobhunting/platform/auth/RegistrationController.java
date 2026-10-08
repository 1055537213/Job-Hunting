package com.jobhunting.platform.auth;

import java.util.List;

import com.jobhunting.platform.billing.InternalTokenVerifier;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

@RestController
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class RegistrationController {
    private final RegistrationService registrations;
    private final InternalTokenVerifier tokens;

    public RegistrationController(RegistrationService registrations, InternalTokenVerifier tokens) {
        this.registrations = registrations;
        this.tokens = tokens;
    }

    @PostMapping("/internal/v1/auth/register")
    public Result register(
            @Valid @RequestBody RegistrationRequest request,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return new Result(registrations.register(
                request.email(),
                request.password(),
                request.display_name(),
                Boolean.TRUE.equals(request.email_verified()),
                request.consents() == null ? List.of() : request.consents()));
    }

    public record RegistrationRequest(
            @NotBlank @Email @Size(max = 254) String email,
            @NotBlank @Size(min = 8, max = 1024) String password,
            @Size(max = 128) String display_name,
            Boolean email_verified,
            List<Consent> consents) {
        @Override
        public String toString() { return "RegistrationRequest[redacted]"; }
    }

    public record Consent(
            String document_type,
            String version,
            @Size(max = 64) String ip_address,
            @Size(max = 512) String user_agent) { }

    public record Result(long account_id) { }
}
