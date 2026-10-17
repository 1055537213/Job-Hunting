package com.jobhunting.platform.auth;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;

@RestController
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class PublicAccountExportController {
    private final AccountExportService exports;

    public PublicAccountExportController(AccountExportService exports) { this.exports = exports; }

    @GetMapping("/api/account/export")
    public JsonNode export(HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        String session = PublicAuthController.cookie(request, PublicAuthController.SESSION_COOKIE);
        if (session == null || session.isBlank() || session.length() > 128) {
            throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
        }
        var result = exports.export(session);
        response.setHeader("Content-Disposition", "attachment; filename=\"job-agent-account-"
                + result.path("account").path("id").asLong() + "-export.json\"");
        return result;
    }
}
