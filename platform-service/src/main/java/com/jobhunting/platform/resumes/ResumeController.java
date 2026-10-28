package com.jobhunting.platform.resumes;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import com.jobhunting.platform.security.WebSecurityFilter;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.CookieValue;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;
import tools.jackson.databind.JsonNode;

@RestController
@RequestMapping
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class ResumeController {
    private final AccountAdministrationService accounts;
    private final ResumeClient python;
    private final InternalTokenVerifier tokens;

    public ResumeController(AccountAdministrationService accounts, ResumeClient python, InternalTokenVerifier tokens) {
        this.accounts = accounts;
        this.python = python;
        this.tokens = tokens;
    }

    @PostMapping(value = "/api/resumes/upload", consumes = "multipart/form-data")
    public JsonNode upload(@CookieValue(name = "job_agent_session", required = false) String session,
            @RequestParam("candidate_id") long candidateId, @RequestPart("file") MultipartFile file,
            HttpServletRequest request, HttpServletResponse response) {
        return submit(session, candidateId, file, request, response);
    }

    @PostMapping(value = "/internal/v1/resumes/upload", consumes = "multipart/form-data")
    public JsonNode internalUpload(@RequestParam("session_token") String session,
            @RequestParam("candidate_id") long candidateId, @RequestPart("file") MultipartFile file,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token,
            HttpServletRequest request, HttpServletResponse response) {
        tokens.verify(token);
        return submit(session, candidateId, file, request, response);
    }

    private JsonNode submit(String session, long candidateId, MultipartFile file,
            HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        if (session == null || session.isBlank() || session.length() > 128 || candidateId <= 0 || file == null) throw ResumeClient.invalid();
        long owner = accounts.current(session).account().account_id();
        return python.upload(session, owner, candidateId, file,
                WebSecurityFilter.requestId(request.getHeader("X-Request-ID"), request.getHeader("X-Trace-Id")));
    }
}
