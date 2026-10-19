package com.jobhunting.platform.profiles;

import java.nio.charset.StandardCharsets;
import java.util.Set;
import java.util.UUID;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.AuthException;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import com.jobhunting.platform.security.WebSecurityFilter;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;
import tools.jackson.databind.JsonNode;

@RestController
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class ProfileCreateController {
    private static final Set<String> INPUT_FIELDS = Set.of("name", "status", "education", "experience_years", "skills",
            "preferred_cities", "acceptable_cities", "salary_floor_k", "expected_salary_k", "target_directions", "unacceptable");
    private final AccountAdministrationService accounts;
    private final ProfileQueryClient python;
    private final InternalTokenVerifier tokens;

    public ProfileCreateController(AccountAdministrationService accounts, ProfileQueryClient python, InternalTokenVerifier tokens) {
        this.accounts = accounts; this.python = python; this.tokens = tokens;
    }

    @PostMapping("/api/profiles")
    public JsonNode create(@CookieValue(name = "job_agent_session", required = false) String session,
            @RequestHeader(name = "Idempotency-Key", required = false) String key, @RequestBody JsonNode profile,
            HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        return submit(session, key == null ? UUID.randomUUID().toString() : key, profile, request.getHeader("X-Request-ID"));
    }

    @PostMapping("/internal/v1/profiles/create")
    public JsonNode internal(@RequestBody JsonNode body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token,
            HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        tokens.verify(token);
        if (!body.isObject() || !body.propertyNames().equals(Set.of("session_token", "idempotency_key", "profile"))
                || !body.path("session_token").isTextual() || !body.path("idempotency_key").isTextual()) throw invalid();
        return submit(body.get("session_token").asText(), body.get("idempotency_key").asText(), body.get("profile"), request.getHeader("X-Trace-Id"));
    }

    private JsonNode submit(String session, String key, JsonNode profile, String requestId) {
        if (session == null || session.isBlank() || session.length() > 128)
            throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
        if (key == null || !key.matches("[A-Za-z0-9:_-]{1,128}")) throw invalid();
        if (!profile.isObject() || !INPUT_FIELDS.containsAll(profile.propertyNames()) || !profile.path("name").isTextual()) throw invalid();
        if (profile.toString().getBytes(StandardCharsets.UTF_8).length > 64 * 1024)
            throw new AuthException("PROFILE_CREATE_TOO_LARGE", "档案内容过大，请精简后重试。", HttpStatus.CONTENT_TOO_LARGE);
        // No transaction spans HTTP; Python revalidates the real session and owns all domain writes.
        long owner = accounts.current(session).account().account_id();
        return python.create(session, owner, key, profile, WebSecurityFilter.requestId(requestId, null));
    }

    private static AuthException invalid() { return new AuthException("INVALID_REQUEST", "请求参数无效。", HttpStatus.UNPROCESSABLE_CONTENT); }
}
