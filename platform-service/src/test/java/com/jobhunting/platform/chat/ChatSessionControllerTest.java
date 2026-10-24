package com.jobhunting.platform.chat;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.PublicAuthErrorHandler;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import com.jobhunting.platform.security.*;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import jakarta.servlet.http.Cookie;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.test.web.servlet.MockMvc;
import tools.jackson.databind.ObjectMapper;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import static org.springframework.test.web.servlet.setup.MockMvcBuilders.standaloneSetup;

class ChatSessionControllerTest {
    final AccountAdministrationService accounts = mock(AccountAdministrationService.class);
    final ChatSessionClient python = mock(ChatSessionClient.class);
    final RedisRequestLimiter limiter = mock(RedisRequestLimiter.class);
    final ObjectMapper mapper = new ObjectMapper();
    final Cookie session = new Cookie("job_agent_session", "real-session");
    final Cookie csrf = new Cookie("job_agent_csrf", "csrf");
    MockMvc mvc;
    @BeforeEach void setup() {
        when(accounts.current("real-session")).thenReturn(new AccountAdministrationService.AccountResponse(
                new AccountAdministrationService.AccountView(42L,"owner@example.com","Owner","user","active",false,null,null,"now","now")));
        var result = mapper.createObjectNode(); result.putArray("sessions");
        when(python.execute(anyString(), eq("real-session"), eq(42L), any(), anyString())).thenReturn(result);
        when(python.invoke(eq("real-session"), eq(42L), any(), anyString())).thenReturn(mapper.createObjectNode().put("reply", "Done"));
        var filter = new WebSecurityFilter(accounts, limiter,
            new WebSecuritySettings(true,"development",true,false,"http://localhost:8000","test:chat",60,20,120,120,240),
            mapper,new SimpleMeterRegistry(),new ClientAddressResolver(""));
        mvc = standaloneSetup(new ChatSessionController(accounts, python, new InternalTokenVerifier("service-secret"), mapper),
                new ChatMessageController(accounts, python, new InternalTokenVerifier("service-secret")))
            .setControllerAdvice(new PublicAuthErrorHandler()).addFilter(filter,"/api/*").build();
    }
    @Test void sessionAndCsrfAreRequiredBeforeCallingPython() throws Exception {
        mvc.perform(get("/api/chat/sessions")).andExpect(status().isUnauthorized());
        mvc.perform(post("/api/chat/sessions").cookie(session).contentType("application/json").content("{\"candidate_id\":7}"))
            .andExpect(status().isForbidden());
        mvc.perform(delete("/api/chat/sessions/chat-1").cookie(session)).andExpect(status().isForbidden());
        verifyNoInteractions(python);
    }
    @Test void forwardsOnlyLocatorsAndOwnerResolvedFromSession() throws Exception {
        mvc.perform(get("/api/chat/sessions?candidate_id=7&account_id=999").cookie(session).header("X-Request-ID","chat-trace"))
            .andExpect(status().isOk()).andExpect(header().string("Cache-Control","no-store"));
        verify(python).execute(eq("list"),eq("real-session"),eq(42L),argThat(v -> v.path("candidate_id").asLong()==7 && !v.has("account_id")),eq("chat-trace"));
        mvc.perform(post("/api/chat/sessions").cookie(session,csrf).header("X-CSRF-Token","csrf")
            .contentType("application/json").content("{\"candidate_id\":7,\"title\":\"Interview\"}" )).andExpect(status().isOk());
        mvc.perform(post("/api/chat/sessions/chat-1/archive").cookie(session,csrf).header("X-CSRF-Token","csrf")).andExpect(status().isOk());
        mvc.perform(delete("/api/chat/sessions/chat-1").cookie(session,csrf).header("X-CSRF-Token","csrf")).andExpect(status().isOk());
        mvc.perform(get("/api/chat/history?candidate_id=7&session_id=chat-1&limit=5").cookie(session)).andExpect(status().isOk());
    }
    @Test void invalidInputsAndUpstreamOutageNeverReturnSuccessfulEmptyData() throws Exception {
        for (String body : new String[]{"{\"candidate_id\":true}","{\"candidate_id\":0}","{\"candidate_id\":7,\"account_id\":999}"})
            mvc.perform(post("/api/chat/sessions").cookie(session,csrf).header("X-CSRF-Token","csrf").contentType("application/json").content(body))
                .andExpect(status().isUnprocessableContent());
        mvc.perform(get("/api/chat/history?candidate_id=7&limit=501").cookie(session)).andExpect(status().isUnprocessableContent());
        mvc.perform(get("/api/chat/history").cookie(session)).andExpect(status().isUnprocessableContent());
        verifyNoInteractions(python);
        when(python.execute(anyString(),anyString(),anyLong(),any(),anyString())).thenThrow(ChatSessionClient.unavailable());
        mvc.perform(get("/api/chat/sessions").cookie(session)).andExpect(status().isServiceUnavailable())
            .andExpect(jsonPath("$.code").value("CHAT_UNAVAILABLE")).andExpect(header().string("Retry-After","3"));
    }
    @Test void internalCompatibilityEndpointRequiresServiceTokenAndRealSession() throws Exception {
        String body = "{\"session_token\":\"real-session\",\"candidate_id\":7}";
        mvc.perform(post("/internal/v1/chat/sessions/list").contentType("application/json").content(body)).andExpect(status().isUnauthorized());
        verifyNoInteractions(python);
        mvc.perform(post("/internal/v1/chat/sessions/list").header("X-Internal-Service-Token","service-secret")
            .contentType("application/json").content(body)).andExpect(status().isOk());
    }
    @Test void messagesRequireSessionCsrfAndModelRateBudgetBeforeExecution() throws Exception {
        String body = "{\"candidate_id\":7,\"message\":\"Hello\"}";
        mvc.perform(post("/api/chat").contentType("application/json").content(body)).andExpect(status().isUnauthorized());
        mvc.perform(post("/api/chat").cookie(session).contentType("application/json").content(body)).andExpect(status().isForbidden());
        verifyNoInteractions(python);
        mvc.perform(post("/api/chat").cookie(session, csrf).header("X-CSRF-Token","csrf").header("X-Request-ID","message-trace")
                .contentType("application/json").content(body)).andExpect(status().isOk()).andExpect(header().string("Cache-Control","no-store"));
        verify(python).invoke(eq("real-session"), eq(42L), argThat(v -> v.path("use_env_llm").asBoolean() && v.path("auto_rag").asBoolean()
            && !v.has("account_id") && v.path("message").asText().equals("Hello")), eq("message-trace"));
        when(limiter.check(anyString(),eq("model"))).thenReturn(7L);
        mvc.perform(post("/api/chat").cookie(session, csrf).header("X-CSRF-Token","csrf").contentType("application/json").content(body))
            .andExpect(status().isTooManyRequests()).andExpect(header().string("Retry-After","7"));
        verify(python, times(1)).invoke(anyString(), anyLong(), any(), anyString());
    }
    @Test void invalidMessagesCannotInjectAnOwnerOrExecuteThroughInternalEndpoint() throws Exception {
        for (String body : new String[]{"{\"candidate_id\":true,\"message\":\"Hello\"}", "{\"candidate_id\":7,\"message\":\"   \"}",
                "{\"candidate_id\":7,\"message\":\"Hello\",\"account_id\":99}", "{\"candidate_id\":7,\"message\":\"Hello\",\"auto_rag\":\"false\"}",
                "{\"candidate_id\":7,\"message\":\"" + "x".repeat(16001) + "\"}"})
            mvc.perform(post("/api/chat").cookie(session, csrf).header("X-CSRF-Token","csrf").contentType("application/json").content(body))
                .andExpect(status().isUnprocessableContent());
        String internal = "{\"session_token\":\"real-session\",\"candidate_id\":7,\"message\":\"Hello\"}";
        mvc.perform(post("/internal/v1/chat/invoke").contentType("application/json").content(internal)).andExpect(status().isUnauthorized());
        verifyNoInteractions(python);
        mvc.perform(post("/internal/v1/chat/invoke").header("X-Internal-Service-Token","service-secret").contentType("application/json").content(internal))
            .andExpect(status().isOk());
    }
    @Test void ambiguousMessagesHaveNoRetryHintOrSuccessfulEmptyResponse() throws Exception {
        when(python.invoke(anyString(),anyLong(),any(),anyString())).thenThrow(ChatSessionClient.unconfirmed());
        mvc.perform(post("/api/chat").cookie(session,csrf).header("X-CSRF-Token","csrf").contentType("application/json")
                .content("{\"candidate_id\":7,\"message\":\"Hello\"}"))
            .andExpect(status().isServiceUnavailable()).andExpect(jsonPath("$.code").value("CHAT_EXECUTION_UNCONFIRMED"))
            .andExpect(header().doesNotExist("Retry-After"));
        verify(python,times(1)).invoke(anyString(),anyLong(),any(),anyString());
    }
}
