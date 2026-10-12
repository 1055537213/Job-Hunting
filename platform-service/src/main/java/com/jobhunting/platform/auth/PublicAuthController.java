package com.jobhunting.platform.auth;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;

import jakarta.servlet.http.Cookie;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpHeaders;
import org.springframework.http.ResponseCookie;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * Same-origin browser facade for the first migrated authentication slice.
 * Internal controllers remain token-protected; this controller is only exposed
 * through the reverse proxy for the explicitly migrated paths.
 */
@RestController
@RequestMapping("/api")
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class PublicAuthController {
    static final String SESSION_COOKIE = "job_agent_session";
    static final String CSRF_COOKIE = "job_agent_csrf";
    static final String CSRF_HEADER = "X-CSRF-Token";
    private static final long SESSION_MAX_AGE_SECONDS = 30L * 24 * 60 * 60;
    private static final long CSRF_MAX_AGE_SECONDS = SESSION_MAX_AGE_SECONDS;
    private static final SecureRandom RANDOM = new SecureRandom();

    private final SessionService sessions;
    private final AccountAdministrationService accounts;
    private final boolean emailVerificationRequired;
    private final boolean cookieSecure;
    private final boolean csrfEnabled;
    public PublicAuthController(
            SessionService sessions,
            AccountAdministrationService accounts,
            @Value("${JOB_AGENT_EMAIL_VERIFICATION_REQUIRED:false}") boolean emailVerificationRequired,
            @Value("${JOB_AGENT_COOKIE_SECURE:false}") boolean cookieSecure,
            @Value("${JOB_AGENT_CSRF_ENABLED:true}") boolean csrfEnabled) {
        this.sessions = sessions;
        this.accounts = accounts;
        this.emailVerificationRequired = emailVerificationRequired;
        this.cookieSecure = cookieSecure;
        this.csrfEnabled = csrfEnabled;
    }

    @PostMapping("/auth/login")
    public Map<String, Object> login(
            @Valid @RequestBody LoginRequest body,
            HttpServletRequest request,
            HttpServletResponse response) {
        String email = body.email().strip().toLowerCase(java.util.Locale.ROOT);
        SessionService.Login login = sessions.login(
                email,
                body.password(),
                emailVerificationRequired,
                header(request, "User-Agent", 1024),
                request.getRemoteAddr());
        AccountAdministrationService.AccountView account = accounts.current(login.session_token()).account();
        String csrfToken = randomToken();
        addCookie(response, SESSION_COOKIE, login.session_token(), true, SESSION_MAX_AGE_SECONDS);
        addCookie(response, CSRF_COOKIE, csrfToken, false, CSRF_MAX_AGE_SECONDS);

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("account", publicAccount(account));
        result.put("csrf_token", csrfToken);
        return result;
    }

    @PostMapping("/auth/logout")
    public Map<String, Boolean> logout(HttpServletRequest request, HttpServletResponse response) {
        String sessionToken = cookie(request, SESSION_COOKIE);
        if (sessionToken != null && !sessionToken.isBlank()) {
            requireCsrf(request);
            sessions.logout(sessionToken);
        }
        deleteCookies(response);
        return Map.of("ok", true);
    }

    @PostMapping("/auth/logout-all")
    public Map<String, Object> logoutAll(HttpServletRequest request, HttpServletResponse response) {
        requireCsrf(request);
        String sessionToken = requiredCookie(request, SESSION_COOKIE);
        int revoked = sessions.logoutAll(sessionToken, header(request, "X-Request-ID", 128));
        deleteCookies(response);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("ok", true);
        result.put("revoked_sessions", revoked);
        return result;
    }

    @PostMapping("/account/password")
    public Map<String, Boolean> changePassword(
            @Valid @RequestBody PasswordRequest body,
            HttpServletRequest request,
            HttpServletResponse response) {
        requireCsrf(request);
        sessions.changePassword(requiredCookie(request, SESSION_COOKIE), body.current_password(), body.new_password());
        deleteCookies(response);
        return Map.of("ok", true);
    }

    private void requireCsrf(HttpServletRequest request) {
        if (!csrfEnabled) return;
        String cookieToken = cookie(request, CSRF_COOKIE);
        String headerToken = request.getHeader(CSRF_HEADER);
        if (cookieToken == null || headerToken == null || cookieToken.isBlank()
                || !MessageDigest.isEqual(
                        cookieToken.getBytes(StandardCharsets.UTF_8),
                        headerToken.getBytes(StandardCharsets.UTF_8))) {
            throw new AuthException(
                    "CSRF_REJECTED",
                    "CSRF 校验失败，请刷新页面后重试。",
                    org.springframework.http.HttpStatus.FORBIDDEN);
        }
    }

    private void addCookie(HttpServletResponse response, String name, String value, boolean httpOnly, long maxAge) {
        response.addHeader(HttpHeaders.SET_COOKIE, ResponseCookie.from(name, value)
                .httpOnly(httpOnly)
                .secure(cookieSecure)
                .sameSite("Lax")
                .path("/")
                .maxAge(maxAge)
                .build()
                .toString());
    }

    private void deleteCookies(HttpServletResponse response) {
        addCookie(response, SESSION_COOKIE, "", true, 0);
        addCookie(response, CSRF_COOKIE, "", false, 0);
    }

    private static String randomToken() {
        byte[] bytes = new byte[32];
        RANDOM.nextBytes(bytes);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
    }

    static String cookie(HttpServletRequest request, String name) {
        if (request.getCookies() == null) return null;
        for (Cookie cookie : request.getCookies()) {
            if (name.equals(cookie.getName())) return cookie.getValue();
        }
        return null;
    }

    private static String requiredCookie(HttpServletRequest request, String name) {
        String value = cookie(request, name);
        if (value == null || value.isBlank()) {
            throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。",
                    org.springframework.http.HttpStatus.UNAUTHORIZED);
        }
        return value;
    }

    private static String header(HttpServletRequest request, String name, int maxLength) {
        String value = request.getHeader(name);
        if (value == null) return null;
        return value.substring(0, Math.min(maxLength, value.length()));
    }

    static Map<String, Object> publicAccount(AccountAdministrationService.AccountView account) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("id", account.account_id());
        result.put("email", account.email());
        result.put("display_name", account.display_name());
        result.put("role", account.role());
        result.put("status", account.status());
        result.put("must_change_password", account.must_change_password());
        result.put("email_verified_at", account.email_verified_at());
        result.put("deleted_at", account.deleted_at());
        result.put("created_at", account.created_at());
        result.put("updated_at", account.updated_at());
        return result;
    }

    public record LoginRequest(@NotBlank @Size(max = 254) String email,
            @NotBlank @Size(max = 1024) String password) {
        @Override public String toString() { return "LoginRequest[redacted]"; }
    }

    public record PasswordRequest(@NotBlank @Size(max = 1024) String current_password,
            @NotBlank @Size(min = 8, max = 1024) String new_password) {
        @Override public String toString() { return "PasswordRequest[redacted]"; }
    }
}
