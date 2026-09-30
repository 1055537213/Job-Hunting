package com.jobhunting.platform.auth;

import com.jobhunting.platform.billing.InternalTokenVerifier;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

@RestController
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class CredentialController {
    private final CredentialVerifier credentials;
    private final InternalTokenVerifier tokens;

    public CredentialController(CredentialVerifier credentials, InternalTokenVerifier tokens) {
        this.credentials = credentials;
        this.tokens = tokens;
    }

    @PostMapping("/internal/v1/auth/verify-credentials")
    public Result verify(
            @Valid @RequestBody CredentialRequest request,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token) {
        tokens.verify(token);
        return new Result(credentials.verify(request.email(), request.password(), request.email_verification_required()));
    }

    public record CredentialRequest(
            @NotBlank @Size(max = 254) String email,
            @NotBlank @Size(max = 1024) String password,
            boolean email_verification_required) {
        @Override
        public String toString() { return "CredentialRequest[redacted]"; }
    }
    public record Result(long account_id) { }
}
