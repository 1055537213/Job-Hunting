package com.jobhunting.platform.auth;

import com.jobhunting.platform.billing.InternalTokenVerifier;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;

@RestController
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class InternalAccountExportController {
    private final AccountExportService exports;
    private final InternalTokenVerifier tokens;

    public InternalAccountExportController(AccountExportService exports, InternalTokenVerifier tokens) {
        this.exports = exports;
        this.tokens = tokens;
    }

    @PostMapping("/internal/v1/accounts/export")
    public JsonNode export(@Valid @RequestBody ExportRequest body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        tokens.verify(token);
        return exports.export(body.session_token());
    }

    public record ExportRequest(@NotBlank @Size(max = 128) String session_token) {
        @Override public String toString() { return "ExportRequest[redacted]"; }
    }
}
