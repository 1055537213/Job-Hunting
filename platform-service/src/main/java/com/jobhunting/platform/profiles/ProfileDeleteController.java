package com.jobhunting.platform.profiles;

import com.jobhunting.platform.billing.InternalTokenVerifier;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.CookieValue;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;

@RestController
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class ProfileDeleteController {
    private final ProfileReadService profiles;
    private final InternalTokenVerifier tokens;
    public ProfileDeleteController(ProfileReadService profiles, InternalTokenVerifier tokens) { this.profiles = profiles; this.tokens = tokens; }

    @DeleteMapping("/api/profiles/{candidateId}")
    public JsonNode delete(@PathVariable("candidateId") Long id,
            @CookieValue(name = "job_agent_session", required = false) String session,
            HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        var value = profiles.delete(session, id, request.getHeader("X-Request-ID"));
        if ("pending".equals(value.get("cleanup_status").asText())) response.setStatus(202);
        return value;
    }

    @PostMapping("/internal/v1/profiles/delete")
    public JsonNode internal(@Valid @RequestBody Command body,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token,
            HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        tokens.verify(token);
        return profiles.delete(body.session_token(), body.candidate_id(), request.getHeader("X-Trace-Id"));
    }

    public record Command(@NotBlank @Size(max = 128) String session_token, @NotNull @Positive Long candidate_id) {
        @Override public String toString() { return "ProfileDeletion[redacted]"; }
    }
}
