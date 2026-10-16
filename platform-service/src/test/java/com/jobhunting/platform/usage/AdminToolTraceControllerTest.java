package com.jobhunting.platform.usage;

import java.util.List;
import java.util.Map;
import com.jobhunting.platform.auth.PublicAuthErrorHandler;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import jakarta.servlet.http.Cookie;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@WebMvcTest(AdminToolTraceController.class)
@Import({PublicAuthErrorHandler.class, AdminToolTraceControllerTest.Tokens.class})
@TestPropertySource(properties = "platform.auth.enabled=true")
class AdminToolTraceControllerTest {
    @Autowired MockMvc mvc;
    @MockitoBean AdminToolTraceService service;

    @Test void guestsCannotReadListsOrDetails() throws Exception {
        for (String path : List.of("", "/task-id")) mvc.perform(get("/api/admin/tools/traces" + path)).andExpect(status().isUnauthorized());
        verifyNoInteractions(service);
    }

    @Test void listsKeepFiltersPaginationAndOriginalShape() throws Exception {
        when(service.list("opaque", 2L, 1, 4)).thenReturn(new AdminToolTraceQueryService.TracePage(List.of(Map.of("id", 9)), 6, 1, 4, 100, 5));
        mvc.perform(get("/api/admin/tools/traces?account_id=2&limit=1&offset=4&actor_account_id=999").cookie(cookie()))
                .andExpect(status().isOk()).andExpect(jsonPath("$.traces[0].id").value(9))
                .andExpect(jsonPath("$.max_pages").value(5)).andExpect(jsonPath("$.limit").value(1));
        mvc.perform(get("/api/admin/tools/traces").cookie(cookie())).andExpect(status().isOk());
        verify(service).list("opaque", null, 100, 0);
    }

    @Test void detailUsesCookieAndTaskIdentifierOnly() throws Exception {
        when(service.detail("opaque", "task-id")).thenReturn(Map.of("trace", Map.of("root_request_id", "task-id", "trace", Map.of("steps", List.of()))));
        mvc.perform(get("/api/admin/tools/traces/task-id?actor_account_id=999").cookie(cookie()))
                .andExpect(status().isOk()).andExpect(jsonPath("$.trace.root_request_id").value("task-id"))
                .andExpect(jsonPath("$.trace.trace.steps").isArray());
        verify(service).detail("opaque", "task-id");
    }

    @Test void badFiltersAndOversizedIdsAreRejected() throws Exception {
        for (String query : List.of("limit=0", "limit=101", "offset=-1", "account_id=oops")) {
            mvc.perform(get("/api/admin/tools/traces?" + query).cookie(cookie())).andExpect(status().isUnprocessableEntity());
        }
        mvc.perform(get("/api/admin/tools/traces/" + "a".repeat(129)).cookie(cookie())).andExpect(status().isUnprocessableEntity());
        verifyNoInteractions(service);
    }

    @Test void internalReadsNeedServiceTokenAndRealSession() throws Exception {
        String page = "{\"session_token\":\"opaque\",\"limit\":2,\"offset\":4,\"account_id\":2}";
        mvc.perform(post("/internal/v1/admin/tools/traces").contentType("application/json").content(page)).andExpect(status().isUnauthorized());
        mvc.perform(post("/internal/v1/admin/tools/traces/detail").contentType("application/json")
                .content("{\"session_token\":\"opaque\",\"root_request_id\":\"task-id\"}")).andExpect(status().isUnauthorized());
        verifyNoInteractions(service);
        mvc.perform(post("/internal/v1/admin/tools/traces").header("X-Internal-Service-Token", "tools-secret")
                .contentType("application/json").content(page)).andExpect(status().isOk());
        verify(service).list("opaque", 2L, 2, 4);
        mvc.perform(post("/internal/v1/admin/tools/traces/detail").header("X-Internal-Service-Token", "tools-secret")
                .contentType("application/json").content("{\"session_token\":\"opaque\",\"root_request_id\":\"task-id\",\"actor_account_id\":999}"))
                .andExpect(status().isOk());
        verify(service).detail("opaque", "task-id");
    }

    private Cookie cookie() { return new Cookie("job_agent_session", "opaque"); }

    @TestConfiguration static class Tokens {
        @Bean InternalTokenVerifier verifier() { return new InternalTokenVerifier("tools-secret"); }
    }
}
