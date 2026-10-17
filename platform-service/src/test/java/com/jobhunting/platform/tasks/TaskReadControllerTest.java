package com.jobhunting.platform.tasks;

import java.util.List;
import java.util.Map;
import com.jobhunting.platform.auth.AuthException;
import com.jobhunting.platform.auth.PublicAuthErrorHandler;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import jakarta.servlet.http.Cookie;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.http.HttpStatus;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@WebMvcTest(TaskReadController.class)
@Import({PublicAuthErrorHandler.class, TaskReadControllerTest.Tokens.class})
@TestPropertySource(properties = "platform.auth.enabled=true")
class TaskReadControllerTest {
    @Autowired MockMvc mvc;
    @MockitoBean TaskReadService service;

    @Test void guestsCannotReadTasks() throws Exception {
        mvc.perform(get("/api/admin/tasks")).andExpect(status().isUnauthorized());
        mvc.perform(get("/api/tasks/task-1")).andExpect(status().isUnauthorized());
        verifyNoInteractions(service);
    }

    @Test void adminKeepsPaginationAndFilters() throws Exception {
        when(service.adminList("opaque", 2L, " probe ", "failed", 1, 4))
                .thenReturn(new TaskQueryService.TaskPage(List.of(Map.of("task_key", "task-1")), 6, 1, 4, 100, 5));
        mvc.perform(get("/api/admin/tasks").param("account_id", "2").param("task_type", " probe ")
                .param("status", "failed").param("limit", "1").param("offset", "4").cookie(cookie()))
                .andExpect(status().isOk()).andExpect(jsonPath("$.tasks[0].task_key").value("task-1"))
                .andExpect(jsonPath("$.page_size").value(100)).andExpect(jsonPath("$.max_pages").value(5));
        mvc.perform(get("/api/admin/tasks").cookie(cookie())).andExpect(status().isOk());
        verify(service).adminList("opaque", null, null, null, 100, 0);
    }

    @Test void personalNeverAcceptsClaimedOwner() throws Exception {
        when(service.personal("opaque", "task-1")).thenReturn(Map.of("task", Map.of("task_key", "task-1", "result", Map.of())));
        mvc.perform(get("/api/tasks/task-1?account_id=999&actor_account_id=999").cookie(cookie()))
                .andExpect(status().isOk()).andExpect(jsonPath("$.task.account_id").doesNotExist())
                .andExpect(jsonPath("$.task.payload").doesNotExist());
        verify(service).personal("opaque", "task-1");
    }

    @Test void invalidParametersAreRejectedBeforeService() throws Exception {
        for (String query : List.of("account_id=0", "account_id=oops", "limit=101", "limit=0", "offset=-1", "task_type=", "status=")) {
            mvc.perform(get("/api/admin/tasks?" + query).cookie(cookie())).andExpect(status().isUnprocessableEntity());
        }
        mvc.perform(get("/api/tasks/" + "a".repeat(65)).cookie(cookie())).andExpect(status().isUnprocessableEntity());
        verifyNoInteractions(service);
    }

    @Test void invalidStatusKeepsBusiness400Contract() throws Exception {
        when(service.adminList("opaque", null, null, "unknown", 100, 0))
                .thenThrow(new AuthException("INVALID_TASK_STATUS", "无效的后台任务状态。", HttpStatus.BAD_REQUEST));
        mvc.perform(get("/api/admin/tasks?status=unknown").cookie(cookie()))
                .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_TASK_STATUS"));
    }

    @Test void internalReadsRequireTokenAndRealSession() throws Exception {
        String admin = "{\"session_token\":\"opaque\",\"limit\":2,\"offset\":4,\"account_id\":2}";
        String personal = "{\"session_token\":\"opaque\",\"task_key\":\"task-1\",\"account_id\":999}";
        mvc.perform(post("/internal/v1/tasks/admin/query").contentType("application/json").content(admin)).andExpect(status().isUnauthorized());
        mvc.perform(post("/internal/v1/tasks/me/query").contentType("application/json").content(personal)).andExpect(status().isUnauthorized());
        verifyNoInteractions(service);
        mvc.perform(post("/internal/v1/tasks/admin/query").header("X-Internal-Service-Token", "task-read-secret")
                .contentType("application/json").content(admin)).andExpect(status().isOk());
        verify(service).adminList("opaque", 2L, null, null, 2, 4);
        mvc.perform(post("/internal/v1/tasks/me/query").header("X-Internal-Service-Token", "task-read-secret")
                .contentType("application/json").content(personal)).andExpect(status().isOk());
        verify(service).personal("opaque", "task-1");
    }

    private Cookie cookie() { return new Cookie("job_agent_session", "opaque"); }
    @TestConfiguration static class Tokens {
        @Bean InternalTokenVerifier verifier() { return new InternalTokenVerifier("task-read-secret"); }
    }
}
