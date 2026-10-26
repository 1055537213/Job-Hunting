package com.jobhunting.platform.projects;

import com.jobhunting.platform.auth.*;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import com.jobhunting.platform.security.*;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import jakarta.servlet.http.Cookie;
import org.junit.jupiter.api.*;
import org.springframework.test.web.servlet.MockMvc;
import tools.jackson.databind.ObjectMapper;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import static org.springframework.test.web.servlet.setup.MockMvcBuilders.standaloneSetup;

class ProjectControllerTest {
    final AccountAdministrationService accounts=mock(AccountAdministrationService.class);
    final ProjectClient python=mock(ProjectClient.class);
    final RedisRequestLimiter limiter=mock(RedisRequestLimiter.class);
    final ObjectMapper mapper=new ObjectMapper();
    final Cookie session=new Cookie("job_agent_session","real-session"),csrf=new Cookie("job_agent_csrf","csrf");
    MockMvc mvc;
    @BeforeEach void setup(){
        when(accounts.current("real-session")).thenReturn(new AccountAdministrationService.AccountResponse(
            new AccountAdministrationService.AccountView(42L,"owner@example.com","Owner","user","active",false,null,null,"now","now")));
        when(python.execute(anyString(),eq("real-session"),eq(42L),any(),anyString())).thenReturn(mapper.createObjectNode());
        var filter=new WebSecurityFilter(accounts,limiter,new WebSecuritySettings(true,"development",true,false,"http://localhost:8000","test:project",60,20,120,120,240),
            mapper,new SimpleMeterRegistry(),new ClientAddressResolver(""));
        mvc=standaloneSetup(new ProjectController(accounts,python,new InternalTokenVerifier("secret"),mapper))
            .setControllerAdvice(new PublicAuthErrorHandler()).addFilter(filter,"/api/*").build();
    }
    @Test void authenticationCsrfAndInternalTokenPrecedeCallbacks()throws Exception{
        mvc.perform(get("/api/projects?candidate_id=7")).andExpect(status().isUnauthorized());
        mvc.perform(post("/api/projects/9/confirm").cookie(session).contentType("application/json").content("{}" )).andExpect(status().isForbidden());
        mvc.perform(delete("/api/projects/9").cookie(session)).andExpect(status().isForbidden());
        mvc.perform(post("/internal/v1/projects/delete").contentType("application/json").content("{\"session_token\":\"real-session\",\"record_id\":9}" )).andExpect(status().isUnauthorized());
        verifyNoInteractions(python);
    }
    @Test void forwardsOnlyLocatorsAndActualSessionOwner()throws Exception{
        mvc.perform(get("/api/projects?candidate_id=7&account_id=99").cookie(session).header("X-Request-ID","project-trace"))
            .andExpect(status().isOk()).andExpect(header().string("Cache-Control","no-store"));
        verify(python).execute(eq("list"),eq("real-session"),eq(42L),argThat(v->v.path("candidate_id").asLong()==7&&!v.has("account_id")),eq("project-trace"));
        mvc.perform(post("/api/projects/9/confirm").cookie(session,csrf).header("X-CSRF-Token","csrf").contentType("application/json")
            .content("{\"confirmed_summary\":\"My contribution\"}" )).andExpect(status().isOk());
        mvc.perform(delete("/api/projects/9").cookie(session,csrf).header("X-CSRF-Token","csrf")).andExpect(status().isOk());
        mvc.perform(post("/internal/v1/projects/delete").header("X-Internal-Service-Token","secret").contentType("application/json")
            .content("{\"session_token\":\"real-session\",\"record_id\":9}" )).andExpect(status().isOk());
    }
    @Test void invalidInputAndOutageCannotExecuteOrLookSuccessful()throws Exception{
        mvc.perform(get("/api/projects?candidate_id=-1").cookie(session)).andExpect(status().isUnprocessableContent());
        mvc.perform(get("/api/projects").cookie(session)).andExpect(status().isUnprocessableContent());
        for(String data:new String[]{"{\"record_id\":99}","{\"account_id\":99}","{\"confirmed_summary\":true}","{\"root_request_id\":\"bad\"}","{\"confirmed_summary\":\""+"x".repeat(20001)+"\"}"})
            mvc.perform(post("/api/projects/9/confirm").cookie(session,csrf).header("X-CSRF-Token","csrf").contentType("application/json").content(data))
                .andExpect(status().isUnprocessableContent());
        verifyNoInteractions(python);
        when(python.execute(anyString(),anyString(),anyLong(),any(),anyString())).thenThrow(ProjectClient.unavailable());
        mvc.perform(delete("/api/projects/9").cookie(session,csrf).header("X-CSRF-Token","csrf"))
            .andExpect(status().isServiceUnavailable()).andExpect(jsonPath("$.code").value("PROJECT_UNAVAILABLE"));
    }
    @Test void githubSubmissionRequiresSessionCsrfAndStrictPublicRepositoryInput()throws Exception{
        String data="{\"candidate_id\":7,\"repository_url\":\"https://github.com/example/project\",\"idempotency_key\":\"github:request-0001\"}";
        mvc.perform(post("/api/projects/github").contentType("application/json").content(data)).andExpect(status().isUnauthorized());
        mvc.perform(post("/api/projects/github").cookie(session).contentType("application/json").content(data)).andExpect(status().isForbidden());
        for(String url:new String[]{"https://gitlab.com/a/b","https://github.com@evil.test/a/b","https://github.com/a/b?token=private","https://github.com/a/b/tree/main"})
            mvc.perform(post("/api/projects/github").cookie(session,csrf).header("X-CSRF-Token","csrf").contentType("application/json")
                .content(mapper.writeValueAsString(mapper.createObjectNode().put("candidate_id",7).put("repository_url",url))))
                .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("PROJECT_REPOSITORY_INVALID"));
        for(String invalid:new String[]{"{\"candidate_id\":true,\"repository_url\":\"https://github.com/a/b\"}",data.replace("7,","7,\"account_id\":99,"),data.replace("github:request-0001","short")})
            mvc.perform(post("/api/projects/github").cookie(session,csrf).header("X-CSRF-Token","csrf").contentType("application/json").content(invalid))
                .andExpect(status().isUnprocessableContent());
        verifyNoInteractions(python);
        mvc.perform(post("/api/projects/github").cookie(session,csrf).header("X-CSRF-Token","csrf").contentType("application/json").content(data)).andExpect(status().isOk());
        verify(python).execute(eq("github"),eq("real-session"),eq(42L),argThat(v->v.path("candidate_id").asLong()==7&&v.path("idempotency_key").asText().equals("github:request-0001")),anyString());
    }
    @Test void localManagementValidatesMetadataAndDoesNotAcceptFileContent()throws Exception{
        String valid="{\"candidate_id\":7,\"project_name\":\"Design\",\"files\":[{\"relative_path\":\"README.md\",\"file_size\":9,\"sha256\":\""+"a".repeat(64)+"\"}]}";
        mvc.perform(post("/api/projects/local/manifest").contentType("application/json").content(valid)).andExpect(status().isUnauthorized());
        mvc.perform(post("/api/projects/local/manifest").cookie(session).contentType("application/json").content(valid)).andExpect(status().isForbidden());
        mvc.perform(delete("/api/projects/local/8").cookie(session)).andExpect(status().isForbidden());
        for(String data:new String[]{valid.replace("\"file_size\":9","\"file_size\":true"),valid.replace("\"file_size\":9","\"file_size\":-1"),
            valid.replace("\"file_size\":9","\"content\":\"private\",\"file_size\":9"),valid.replace("\"candidate_id\":7","\"candidate_id\":7,\"account_id\":99"),valid.replace("Design"," ")})
            mvc.perform(post("/api/projects/local/manifest").cookie(session,csrf).header("X-CSRF-Token","csrf").contentType("application/json").content(data)).andExpect(status().isUnprocessableContent());
        mvc.perform(get("/api/projects/local/-1").cookie(session)).andExpect(status().isUnprocessableContent());
        verifyNoInteractions(python);
        mvc.perform(post("/api/projects/local/manifest").cookie(session,csrf).header("X-CSRF-Token","csrf").contentType("application/json").content(valid)).andExpect(status().isOk());
        mvc.perform(get("/api/projects/local/8?account_id=99").cookie(session)).andExpect(status().isOk());
        mvc.perform(delete("/api/projects/local/8").cookie(session,csrf).header("X-CSRF-Token","csrf")).andExpect(status().isOk());
        verify(python).execute(eq("local-get"),eq("real-session"),eq(42L),argThat(n->n.path("collection_id").asLong()==8&&!n.has("account_id")),anyString());
        verify(python).execute(eq("local-cancel"),eq("real-session"),eq(42L),any(),anyString());
    }
    @Test void localCompletionUsesOnlySessionAndPathWithCsrfAndNoInjectedInput()throws Exception{
        String path="/api/projects/local/8/complete";
        mvc.perform(post(path)).andExpect(status().isUnauthorized());
        mvc.perform(post(path).cookie(session)).andExpect(status().isForbidden());
        mvc.perform(post("/api/projects/local/-1/complete").cookie(session,csrf).header("X-CSRF-Token","csrf"))
            .andExpect(status().isUnprocessableContent());
        for(String body:new String[]{"[]","{\"account_id\":99}","{\"collection_id\":99}","{\"files\":[]}"})
            mvc.perform(post(path).cookie(session,csrf).header("X-CSRF-Token","csrf").contentType("application/json").content(body))
                .andExpect(status().isUnprocessableContent());
        mvc.perform(post("/internal/v1/projects/local-complete").contentType("application/json")
            .content("{\"session_token\":\"real-session\",\"collection_id\":8}")).andExpect(status().isUnauthorized());
        verifyNoInteractions(python);
        mvc.perform(post(path).cookie(session,csrf).header("X-CSRF-Token","csrf"))
            .andExpect(status().isOk()).andExpect(header().string("Cache-Control","no-store"));
        verify(python).execute(eq("local-complete"),eq("real-session"),eq(42L),argThat(n->n.propertyNames().equals(java.util.Set.of("collection_id"))&&n.path("collection_id").asLong()==8),anyString());
        when(python.execute(anyString(),anyString(),anyLong(),any(),anyString())).thenThrow(ProjectClient.unavailable());
        mvc.perform(post(path).cookie(session,csrf).header("X-CSRF-Token","csrf"))
            .andExpect(status().isServiceUnavailable()).andExpect(jsonPath("$.code").value("PROJECT_UNAVAILABLE"));
    }
}
