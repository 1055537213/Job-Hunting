package com.jobhunting.platform.jobs;

import java.util.List;
import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import com.jobhunting.platform.security.WebSecurityFilter;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.CookieValue;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;
import tools.jackson.databind.JsonNode;

@RestController
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class JobController {
    private final AccountAdministrationService accounts;
    private final JobClient python;
    private final JobTextClient text;
    private final InternalTokenVerifier tokens;

    public JobController(AccountAdministrationService accounts, JobClient python, JobTextClient text, InternalTokenVerifier tokens) {
        this.accounts = accounts;
        this.python = python;
        this.text = text;
        this.tokens = tokens;
    }

    @PostMapping(value = "/api/jobs", consumes = "application/json")
    public JsonNode uploadText(@CookieValue(name = "job_agent_session", required = false) String session,
            @RequestBody JobTextRequest payload, HttpServletRequest request, HttpServletResponse response) {
        return submitText(session, payload.raw_text(), payload.source_url(), request, response);
    }

    @PostMapping(value = "/internal/v1/jobs/text", consumes = "application/json")
    public JsonNode internalText(@RequestBody InternalJobTextRequest payload,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token,
            HttpServletRequest request, HttpServletResponse response) {
        tokens.verify(token);
        return submitText(payload.session_token(), payload.raw_text(), payload.source_url(), request, response);
    }

    @PostMapping(value = "/api/jobs/screenshots", consumes = "multipart/form-data")
    public JsonNode upload(@CookieValue(name = "job_agent_session", required = false) String session,
            @RequestPart("screenshots") List<MultipartFile> files, @RequestParam(name = "source_url", required = false) String sourceUrl,
            HttpServletRequest request, HttpServletResponse response) {
        return submit(session, files, sourceUrl, request, response);
    }

    @PostMapping(value = "/internal/v1/jobs/screenshots", consumes = "multipart/form-data")
    public JsonNode internalUpload(@RequestParam("session_token") String session,
            @RequestPart("screenshots") List<MultipartFile> files, @RequestParam(name = "source_url", required = false) String sourceUrl,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String token,
            HttpServletRequest request, HttpServletResponse response) {
        tokens.verify(token);
        return submit(session, files, sourceUrl, request, response);
    }

    private JsonNode submit(String session, List<MultipartFile> files, String sourceUrl,
            HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        if (session == null || session.isBlank() || session.length() > 128 || files == null || files.isEmpty() || files.size() > JobClient.MAX_FILES
                || sourceUrl != null && sourceUrl.length() > 2048) throw JobClient.invalid();
        long owner = accounts.current(session).account().account_id();
        return python.upload(session, owner, files, sourceUrl,
                WebSecurityFilter.requestId(request.getHeader("X-Request-ID"), request.getHeader("X-Trace-Id")));
    }

    private JsonNode submitText(String session, String rawText, String sourceUrl,
            HttpServletRequest request, HttpServletResponse response) {
        response.setHeader("Cache-Control", "no-store");
        if (session == null || session.isBlank() || session.length() > 128 || rawText == null || rawText.isBlank()
                || rawText.length() > JobTextClient.MAX_RAW_TEXT_CHARS
                || sourceUrl != null && sourceUrl.length() > JobTextClient.MAX_SOURCE_URL_CHARS) throw JobTextClient.invalid();
        long owner = accounts.current(session).account().account_id();
        return text.importText(session, owner, rawText, sourceUrl,
                WebSecurityFilter.requestId(request.getHeader("X-Request-ID"), request.getHeader("X-Trace-Id")));
    }

    public record JobTextRequest(String raw_text, String source_url) {}
    public record InternalJobTextRequest(String session_token, String raw_text, String source_url) {}
}
