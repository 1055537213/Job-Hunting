package com.jobhunting.platform.resumes;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.PublicAuthErrorHandler;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import com.jobhunting.platform.security.ClientAddressResolver;
import com.jobhunting.platform.security.RedisRequestLimiter;
import com.jobhunting.platform.security.WebSecurityFilter;
import com.jobhunting.platform.security.WebSecuritySettings;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import jakarta.servlet.http.Cookie;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.test.web.servlet.MockMvc;
import tools.jackson.databind.ObjectMapper;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.multipart;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import static org.springframework.test.web.servlet.setup.MockMvcBuilders.standaloneSetup;

class ResumeControllerTest {
    final AccountAdministrationService accounts = mock(AccountAdministrationService.class);
    final ResumeClient python = mock(ResumeClient.class);
    final RedisRequestLimiter limiter = mock(RedisRequestLimiter.class);
    final ObjectMapper mapper = new ObjectMapper();
    final Cookie session = new Cookie("job_agent_session", "real-session");
    final Cookie csrf = new Cookie("job_agent_csrf", "csrf");
    MockMvc mvc;

    @BeforeEach void setup() {
        when(accounts.current("real-session")).thenReturn(new AccountAdministrationService.AccountResponse(
            new AccountAdministrationService.AccountView(42L, "owner@example.com", "Owner", "user", "active", false, null, null, "now", "now")));
        when(python.upload(anyString(), eq(42L), eq(7L), any(), anyString())).thenReturn(mapper.createObjectNode().put("ok", true));
        var filter = new WebSecurityFilter(accounts, limiter,
            new WebSecuritySettings(true, "development", true, false, "http://localhost:8000", "test:resume", 60, 20, 120, 120, 240),
            mapper, new SimpleMeterRegistry(), new ClientAddressResolver(""));
        mvc = standaloneSetup(new ResumeController(accounts, python, new InternalTokenVerifier("secret")))
            .setControllerAdvice(new PublicAuthErrorHandler()).addFilter(filter, "/api/*").build();
    }

    @Test void publicUploadRequiresSessionAndCsrfAndForwardsActualOwner() throws Exception {
        var file = new MockMultipartFile("file", "resume.docx", "application/octet-stream", "resume".getBytes());
        mvc.perform(multipart("/api/resumes/upload").file(file).param("candidate_id", "7"))
            .andExpect(status().isUnauthorized());
        mvc.perform(multipart("/api/resumes/upload").file(file).param("candidate_id", "7").cookie(session))
            .andExpect(status().isForbidden());
        mvc.perform(multipart("/api/resumes/upload").file(file).param("candidate_id", "7").cookie(session, csrf)
                .header("X-CSRF-Token", "csrf").header("X-Request-ID", "resume-trace"))
            .andExpect(status().isOk()).andExpect(header().string("Cache-Control", "no-store"));
        verify(python).upload(eq("real-session"), eq(42L), eq(7L), any(), eq("resume-trace"));
    }

    @Test void internalUploadRequiresServiceTokenAndUsesSameSessionScope() throws Exception {
        var file = new MockMultipartFile("file", "resume.pdf", "application/pdf", "resume".getBytes());
        mvc.perform(multipart("/internal/v1/resumes/upload").file(file).param("candidate_id", "7").param("session_token", "real-session"))
            .andExpect(status().isUnauthorized());
        mvc.perform(multipart("/internal/v1/resumes/upload").file(file).param("candidate_id", "7").param("session_token", "real-session")
                .header("X-Internal-Service-Token", "secret").header("X-Trace-Id", "callback-trace"))
            .andExpect(status().isOk());
        verify(python).upload(eq("real-session"), eq(42L), eq(7L), any(), eq("callback-trace"));
    }

    @Test void invalidCandidateNeverCallsPython() throws Exception {
        var file = new MockMultipartFile("file", "resume.pdf", "application/pdf", "resume".getBytes());
        mvc.perform(multipart("/api/resumes/upload").file(file).param("candidate_id", "0").cookie(session, csrf)
                .header("X-CSRF-Token", "csrf"))
            .andExpect(status().isUnprocessableContent());
        verifyNoInteractions(python);
    }
}
