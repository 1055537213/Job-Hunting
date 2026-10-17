package com.jobhunting.platform.auth;

import java.util.Locale;
import java.util.Map;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

@RestController
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class PublicAccountProfileController {
    private final AccountAdministrationService accounts;
    private final AccountActionEmailService emails;

    public PublicAccountProfileController(AccountAdministrationService accounts, AccountActionEmailService emails) {
        this.accounts = accounts;
        this.emails = emails;
    }

    @PatchMapping("/api/account/profile")
    public Map<String, Object> profile(@Valid @RequestBody ProfileRequest body,
            HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        var updated = accounts.updateProfile(session(request), body.display_name(), request.getHeader("X-Request-ID"));
        return Map.of("account", PublicAuthController.publicAccount(updated.account()));
    }

    @PatchMapping("/api/account/email")
    public Map<String, String> email(@Valid @RequestBody EmailChangeRequest body,
            HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        emails.requestEmailChange(session(request), body.new_email(), request.getRemoteAddr());
        return Map.of("message", "确认邮件已发送，请打开新邮箱中的链接完成变更。");
    }

    private static String session(HttpServletRequest request) {
        String token = PublicAuthController.cookie(request, PublicAuthController.SESSION_COOKIE);
        if (token == null || token.isBlank() || token.length() > 128) {
            throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
        }
        return token;
    }

    public record ProfileRequest(@Size(max = 128) String display_name) {
        @Override public String toString() { return "ProfileRequest[redacted]"; }
    }

    public record EmailChangeRequest(@NotBlank @Email @Size(min = 3, max = 254) String new_email) {
        public EmailChangeRequest {
            if (new_email != null) new_email = new_email.strip().toLowerCase(Locale.ROOT);
        }
        @Override public String toString() { return "EmailChangeRequest[redacted]"; }
    }
}
