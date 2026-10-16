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

@WebMvcTest(AdminUsageController.class)
@Import({PublicAuthErrorHandler.class, AdminUsageControllerTest.Tokens.class})
@TestPropertySource(properties = "platform.auth.enabled=true")
class AdminUsageControllerTest {
    @Autowired MockMvc mvc;
    @MockitoBean AdminUsageService service;

    @Test void guestsCannotReadUsage() throws Exception {
        for (String path : List.of("summary", "events")) {
            mvc.perform(get("/api/admin/usage/" + path)).andExpect(status().isUnauthorized());
        }
        verifyNoInteractions(service);
    }

    @Test void summaryUsesRealCookieNotClaimedActor() throws Exception {
        when(service.summary("opaque")).thenReturn(Map.of("page_size", 100));
        mvc.perform(get("/api/admin/usage/summary?actor_account_id=999").cookie(cookie()))
                .andExpect(status().isOk()).andExpect(jsonPath("$.page_size").value(100));
        verify(service).summary("opaque");
    }

    @Test void eventsPreserveNullableFieldsFiltersAndPagination() throws Exception {
        var event = new AdminUsageQueryService.UsageEvent(3, 2, null, null, null, "call", "provider", "model", "chat",
                80, 20, 100, "provider", "succeeded", 1, null, Map.of("tokens", 100), "2026-10-16T00:00:00+00:00", true, null);
        when(service.events("opaque", 2L, 7L, "a'b", 1, 4)).thenReturn(new AdminUsageQueryService.UsagePage(List.of(event), 6, 1, 4, 100, 5));
        mvc.perform(get("/api/admin/usage/events").param("account_id", "2").param("candidate_id", "7")
                .param("session_id", "a'b").param("limit", "1").param("offset", "4").cookie(cookie()))
                .andExpect(status().isOk()).andExpect(jsonPath("$.events[0].candidate_id").value(org.hamcrest.Matchers.nullValue()))
                .andExpect(jsonPath("$.events[0].raw_usage.tokens").value(100)).andExpect(jsonPath("$.max_pages").value(5));
        mvc.perform(get("/api/admin/usage/events").cookie(cookie())).andExpect(status().isOk());
        verify(service).events("opaque", null, null, null, 100, 0);
    }

    @Test void invalidPagesAndIdsAreRejectedBeforeService() throws Exception {
        for (String query : List.of("limit=0", "limit=101", "offset=-1", "account_id=oops", "candidate_id=9223372036854775808")) {
            mvc.perform(get("/api/admin/usage/events?" + query).cookie(cookie()))
                    .andExpect(status().isUnprocessableEntity()).andExpect(jsonPath("$.detail").isString());
        }
        verifyNoInteractions(service);
    }

    @Test void internalReadsNeedTokenAndRealSession() throws Exception {
        mvc.perform(post("/internal/v1/admin/usage/summary").contentType("application/json").content("{\"session_token\":\"opaque\"}"))
                .andExpect(status().isUnauthorized());
        verifyNoInteractions(service);
        mvc.perform(post("/internal/v1/admin/usage/summary").header("X-Internal-Service-Token", "usage-secret")
                .contentType("application/json").content("{\"session_token\":\"opaque\",\"actor_account_id\":999}"))
                .andExpect(status().isOk());
        verify(service).summary("opaque");
        mvc.perform(post("/internal/v1/admin/usage/events").header("X-Internal-Service-Token", "usage-secret")
                .contentType("application/json").content("{\"session_token\":\"opaque\",\"limit\":2,\"offset\":4,\"account_id\":2}"))
                .andExpect(status().isOk());
        verify(service).events("opaque", 2L, null, null, 2, 4);
    }

    private Cookie cookie() { return new Cookie("job_agent_session", "opaque"); }

    @TestConfiguration static class Tokens {
        @Bean InternalTokenVerifier verifier() { return new InternalTokenVerifier("usage-secret"); }
    }
}
