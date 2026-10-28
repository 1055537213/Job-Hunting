package com.jobhunting.platform.jobs;

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

class JobControllerTest {
    final AccountAdministrationService accounts = mock(AccountAdministrationService.class);
    final JobClient python = mock(JobClient.class);
    final RedisRequestLimiter limiter = mock(RedisRequestLimiter.class);
    final ObjectMapper mapper = new ObjectMapper();
    final Cookie session = new Cookie("job_agent_session", "real-session");
    final Cookie csrf = new Cookie("job_agent_csrf", "csrf");
    MockMvc mvc;

    @BeforeEach void setup() {
        when(accounts.current("real-session")).thenReturn(new AccountAdministrationService.AccountResponse(
            new AccountAdministrationService.AccountView(42L, "owner@example.com", "Owner", "user", "active", false, null, null, "now", "now")));
        when(python.upload(anyString(), eq(42L), anyList(), any(), anyString())).thenReturn(mapper.createObjectNode().put("ok", true));
        var filter = new WebSecurityFilter(accounts, limiter,
            new WebSecuritySettings(true, "development", true, false, "http://localhost:8000", "test:job", 60, 20, 120, 120, 240),
            mapper, new SimpleMeterRegistry(), new ClientAddressResolver(""));
        mvc = standaloneSetup(new JobController(accounts, python, new InternalTokenVerifier("secret")))
            .setControllerAdvice(new PublicAuthErrorHandler()).addFilter(filter, "/api/*").build();
    }

    @Test void publicUploadRequiresSessionAndCsrfAndForwardsActualOwner() throws Exception {
        var file = new MockMultipartFile("screenshots", "job.png", "image/png", "image".getBytes());
        mvc.perform(multipart("/api/jobs/screenshots").file(file))
            .andExpect(status().isUnauthorized());
        mvc.perform(multipart("/api/jobs/screenshots").file(file).cookie(session))
            .andExpect(status().isForbidden());
        mvc.perform(multipart("/api/jobs/screenshots").file(file).param("source_url", "https://example.com/job")
                .cookie(session, csrf).header("X-CSRF-Token", "csrf").header("X-Request-ID", "job-trace"))
            .andExpect(status().isOk()).andExpect(header().string("Cache-Control", "no-store"));
        verify(python).upload(eq("real-session"), eq(42L), anyList(), eq("https://example.com/job"), eq("job-trace"));
    }

    @Test void internalUploadRequiresServiceToken() throws Exception {
        var file = new MockMultipartFile("screenshots", "job.png", "image/png", "image".getBytes());
        mvc.perform(multipart("/internal/v1/jobs/screenshots").file(file).param("session_token", "real-session"))
            .andExpect(status().isUnauthorized());
        mvc.perform(multipart("/internal/v1/jobs/screenshots").file(file).param("session_token", "real-session")
                .header("X-Internal-Service-Token", "secret").header("X-Trace-Id", "callback-trace"))
            .andExpect(status().isOk());
        verify(python).upload(eq("real-session"), eq(42L), anyList(), isNull(), eq("callback-trace"));
    }

    @Test void rejectsMoreThanFourScreenshotsBeforePython() throws Exception {
        var request = multipart("/api/jobs/screenshots").cookie(session, csrf).header("X-CSRF-Token", "csrf");
        for (int index = 0; index < 5; index++) {
            request.file(new MockMultipartFile("screenshots", "job-" + index + ".png", "image/png", "image".getBytes()));
        }
        mvc.perform(request).andExpect(status().isUnprocessableContent());
        verifyNoInteractions(python);
    }
}
