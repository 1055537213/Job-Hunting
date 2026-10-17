package com.jobhunting.platform.tasks;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.AuthException;
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
import org.mockito.ArgumentCaptor;
import org.springframework.http.HttpStatus;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import tools.jackson.databind.ObjectMapper;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

class TaskCommandControllerTest {
    final AccountAdministrationService accounts = mock(AccountAdministrationService.class);
    final TaskCommandClient client = mock(TaskCommandClient.class);
    final RedisRequestLimiter limiter = mock(RedisRequestLimiter.class);
    MockMvc mvc;
    MockMvc guarded;

    @BeforeEach void setup() {
        var controller = new TaskCommandController(new TaskCommandService(accounts, client),
                new InternalTokenVerifier(TaskCommandClientTest.TOKEN));
        mvc = MockMvcBuilders.standaloneSetup(controller).setControllerAdvice(new PublicAuthErrorHandler()).build();
        var settings = new WebSecuritySettings(true, "test", true, false, "http://localhost:8000",
                "test:tasks", 60, 20, 120, 120, 240);
        var filter = new WebSecurityFilter(accounts, limiter, settings, new ObjectMapper(),
                new SimpleMeterRegistry(), new ClientAddressResolver(""));
        guarded = MockMvcBuilders.standaloneSetup(controller).setControllerAdvice(new PublicAuthErrorHandler()).addFilters(filter).build();
        when(accounts.authorizeAdministrator(nullable(String.class))).thenAnswer(call -> {
            if (!TaskCommandClientTest.SESSION.equals(call.getArgument(0))) {
                throw new AuthException("SESSION_EXPIRED", "Session expired.", HttpStatus.UNAUTHORIZED);
            }
            return TaskCommandClientTest.actor(7);
        });
        when(accounts.current(TaskCommandClientTest.SESSION)).thenReturn(new AccountAdministrationService.AccountResponse(TaskCommandClientTest.actor(7)));
        when(client.submit(anyString(), anyString(), nullable(String.class), anyString(), anyString()))
                .thenAnswer(call -> TaskCommandClientTest.response("retry".equals(call.getArgument(1))));
    }

    @Test void publicCommandsUseActualCookieAndNormalizeRequestId() throws Exception {
        mvc.perform(post("/api/admin/tasks/probe").cookie(session()).header("X-Request-ID", "request-id-123")
                .header("Idempotency-Key", "probe-key-123").header("X-Account-ID", "999")
                .contentType("application/json").content("{\"session_token\":\"forged\",\"actor_account_id\":999}"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.task.account_id").doesNotExist())
                .andExpect(jsonPath("$.task.payload").doesNotExist());
        verify(accounts).authorizeAdministrator(TaskCommandClientTest.SESSION);
        verify(client).submit(matches("[a-f0-9]{64}"), eq("probe"), isNull(), eq(TaskCommandClientTest.SESSION), eq("request-id-123"));
        mvc.perform(post("/api/admin/tasks/task-123/retry").cookie(session()).header("X-Trace-Id", "trace-id-123"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.task.account_id").value(20))
                .andExpect(jsonPath("$.task.candidate_id").isEmpty());
        verify(client).submit(anyString(), eq("retry"), eq("task-123"), eq(TaskCommandClientTest.SESSION), eq("trace-id-123"));
        mvc.perform(post("/api/admin/tasks/probe").cookie(session()).header("X-Request-ID", "invalid id"))
                .andExpect(status().isOk());
        var ids = ArgumentCaptor.forClass(String.class);
        verify(client, times(3)).submit(anyString(), anyString(), nullable(String.class), eq(TaskCommandClientTest.SESSION), ids.capture());
        assertThat(ids.getAllValues().get(2)).matches("[A-Za-z0-9._-]{8,128}");
    }

    @Test void guestsExpiredSessionsAndNonAdministratorsNeverCallPython() throws Exception {
        mvc.perform(post("/api/admin/tasks/probe").header("X-Account-ID", "7")
                .contentType("application/json").content("{\"session_token\":\"forged\"}"))
                .andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value("SESSION_EXPIRED"));
        mvc.perform(post("/api/admin/tasks/task-123/retry").cookie(new Cookie("job_agent_session", "expired")))
                .andExpect(status().isUnauthorized());
        when(accounts.authorizeAdministrator(TaskCommandClientTest.SESSION))
                .thenThrow(new AuthException("ADMIN_REQUIRED", "Administrator required.", HttpStatus.FORBIDDEN));
        mvc.perform(post("/api/admin/tasks/probe").cookie(session())).andExpect(status().isForbidden());
        verifyNoInteractions(client);
    }

    @Test void filterRequiresCsrfAndAdminLimitsAndForwardsItsNormalizedId() throws Exception {
        guarded.perform(post("/api/admin/tasks/probe")).andExpect(status().isUnauthorized());
        guarded.perform(post("/api/admin/tasks/probe").cookie(session())).andExpect(status().isForbidden());
        verifyNoInteractions(client);
        var response = guarded.perform(post("/api/admin/tasks/probe").cookie(session(), csrf()).header("X-CSRF-Token", "csrf-token")
                .header("X-Request-ID", "invalid id")).andExpect(status().isOk())
                .andExpect(header().string("Cache-Control", "no-store")).andReturn().getResponse();
        String normalized = response.getHeader("X-Request-ID");
        assertThat(normalized).matches("[A-Za-z0-9._-]{8,128}");
        verify(accounts).authorizeAdministrator(TaskCommandClientTest.SESSION);
        verify(client).submit(anyString(), eq("probe"), isNull(), eq(TaskCommandClientTest.SESSION), eq(normalized));
        when(limiter.check("account:7", "admin")).thenReturn(13L);
        guarded.perform(post("/api/admin/tasks/task-123/retry").cookie(session(), csrf()).header("X-CSRF-Token", "csrf-token"))
                .andExpect(status().isTooManyRequests()).andExpect(header().string("Retry-After", "13"));
        verifyNoMoreInteractions(client);
    }

    @Test void internalCommandsRequireServiceTokenAndRealAdministratorSession() throws Exception {
        String body = "{\"session_token\":\"" + TaskCommandClientTest.SESSION
                + "\",\"operation\":\"retry\",\"task_key\":\"task-123\",\"idempotency_key\":\"retry-key-123\"}";
        mvc.perform(post("/internal/v1/tasks/admin/command").contentType("application/json").content(body))
                .andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value("INTERNAL_UNAUTHORIZED"));
        mvc.perform(post("/internal/v1/tasks/admin/command").header("X-Internal-Service-Token", "wrong")
                .contentType("application/json").content(body)).andExpect(status().isUnauthorized());
        verifyNoInteractions(accounts, client);
        mvc.perform(post("/internal/v1/tasks/admin/command").header("X-Internal-Service-Token", TaskCommandClientTest.TOKEN)
                .header("X-Trace-Id", "trace-id-123").contentType("application/json").content(body))
                .andExpect(status().isOk()).andExpect(jsonPath("$.task.account_id").value(20));
        verify(accounts).authorizeAdministrator(TaskCommandClientTest.SESSION);
        verify(client).submit(anyString(), eq("retry"), eq("task-123"), eq(TaskCommandClientTest.SESSION), eq("trace-id-123"));
        mvc.perform(post("/internal/v1/tasks/admin/command").header("X-Internal-Service-Token", TaskCommandClientTest.TOKEN)
                .contentType("application/json").content(body.replace(TaskCommandClientTest.SESSION, "expired")))
                .andExpect(status().isUnauthorized());
        verifyNoMoreInteractions(client);
    }

    @Test void publicAndCompatibilityCommandsShareTheSameActorScopedId() throws Exception {
        mvc.perform(post("/api/admin/tasks/probe").cookie(session()).header("Idempotency-Key", "shared-key-123"))
                .andExpect(status().isOk());
        mvc.perform(post("/internal/v1/tasks/admin/command").header("X-Internal-Service-Token", TaskCommandClientTest.TOKEN)
                .contentType("application/json").content("{\"session_token\":\"" + TaskCommandClientTest.SESSION
                        + "\",\"operation\":\"retry\",\"task_key\":\"task-123\",\"idempotency_key\":\"shared-key-123\"}"))
                .andExpect(status().isOk());
        var ids = ArgumentCaptor.forClass(String.class);
        verify(client, times(2)).submit(ids.capture(), anyString(), nullable(String.class), anyString(), anyString());
        assertThat(ids.getAllValues().getFirst()).isEqualTo(ids.getAllValues().getLast());
    }

    @Test void malformedPayloadsAndInvalidIntentFailBeforePython() throws Exception {
        for (String body : new String[]{"{", "{}", "{\"session_token\":\"x\"}",
                "{\"session_token\":\"" + TaskCommandClientTest.SESSION + "\",\"operation\":\"delete\"}",
                "{\"session_token\":\"" + TaskCommandClientTest.SESSION + "\",\"operation\":\"retry\"}",
                "{\"session_token\":\"" + TaskCommandClientTest.SESSION + "\",\"operation\":\"probe\",\"task_key\":\"task-123\"}"}) {
            mvc.perform(post("/internal/v1/tasks/admin/command").header("X-Internal-Service-Token", TaskCommandClientTest.TOKEN)
                    .contentType("application/json").content(body)).andExpect(status().is4xxClientError())
                    .andExpect(jsonPath("$.code").value("INVALID_REQUEST"));
        }
        mvc.perform(post("/api/admin/tasks/probe").cookie(session()).header("Idempotency-Key", "short"))
                .andExpect(status().isUnprocessableEntity());
        mvc.perform(post("/api/admin/tasks/" + "t".repeat(65) + "/retry").cookie(session()))
                .andExpect(status().isUnprocessableEntity());
        verifyNoInteractions(client);
    }

    @Test void businessAndPendingErrorsKeepBrowserEnvelopeAndSafeRetryHeader() throws Exception {
        when(client.submit(anyString(), anyString(), nullable(String.class), anyString(), anyString()))
                .thenThrow(new AuthException("TASK_COMMAND_CONFLICT", "Task conflict.", HttpStatus.CONFLICT));
        mvc.perform(post("/api/admin/tasks/task-123/retry").cookie(session()).header("X-Request-ID", "request-id-123"))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("TASK_COMMAND_CONFLICT"))
                .andExpect(jsonPath("$.detail").value("Task conflict."))
                .andExpect(jsonPath("$.message").value("Task conflict."))
                .andExpect(jsonPath("$.request_id").value("request-id-123"))
                .andExpect(jsonPath("$.trace_id").value("request-id-123"));
        doThrow(new AuthException("TASK_COMMAND_UNAVAILABLE", "Task dispatch pending.", HttpStatus.SERVICE_UNAVAILABLE))
                .when(client).submit(anyString(), anyString(), nullable(String.class), anyString(), anyString());
        mvc.perform(post("/api/admin/tasks/probe").cookie(session()))
                .andExpect(status().isServiceUnavailable()).andExpect(header().string("Retry-After", "3"))
                .andExpect(jsonPath("$.code").value("TASK_COMMAND_UNAVAILABLE"));
        assertThat(new TaskCommandController.CommandRequest(TaskCommandClientTest.SESSION, "probe", null, "secret-key").toString())
                .isEqualTo("TaskCommandRequest[redacted]");
    }

    private static Cookie session() { return new Cookie("job_agent_session", TaskCommandClientTest.SESSION); }
    private static Cookie csrf() { return new Cookie("job_agent_csrf", "csrf-token"); }
}
