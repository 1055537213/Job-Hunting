package com.jobhunting.platform.profiles;

import com.jobhunting.platform.billing.InternalTokenVerifier;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.CookieValue;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;

@RestController
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class ProfileReadController {
    private final ProfileReadService profiles;
    private final InternalTokenVerifier tokens;
    public ProfileReadController(ProfileReadService profiles, InternalTokenVerifier tokens) { this.profiles = profiles; this.tokens = tokens; }

    @GetMapping("/api/profiles")
    public JsonNode list(@CookieValue(name = "job_agent_session", required = false) String session,
            HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        return profiles.query(session, null, request.getHeader("X-Request-ID"));
    }

    @GetMapping("/api/profiles/{candidateId}")
    public JsonNode detail(@PathVariable("candidateId") Long id,
            @CookieValue(name = "job_agent_session", required = false) String session, HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        return profiles.query(session, id, request.getHeader("X-Request-ID"));
    }

    @PostMapping("/internal/v1/profiles/query")
    public JsonNode internal(@Valid @RequestBody Query body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token,
            HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        tokens.verify(token);
        return profiles.query(body.session_token(), body.candidate_id(), request.getHeader("X-Trace-Id"));
    }

    public record Query(@NotBlank @Size(max = 128) String session_token, @Positive Long candidate_id) {
        @Override public String toString() { return "ProfileQuery[redacted]"; }
    }
}
