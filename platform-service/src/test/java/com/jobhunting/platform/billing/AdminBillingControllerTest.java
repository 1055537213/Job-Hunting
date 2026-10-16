package com.jobhunting.platform.billing;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;
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
import com.jobhunting.platform.auth.PublicAuthErrorHandler;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@WebMvcTest(AdminBillingController.class)
@Import({PublicAuthErrorHandler.class, AdminBillingControllerTest.Tokens.class})
@TestPropertySource(properties = "platform.auth.enabled=true")
class AdminBillingControllerTest {
    @Autowired MockMvc mvc;
    @MockitoBean AdminBillingService service;

    @Test void guestsCannotReadOrCredit() throws Exception {
        for (String path : List.of("/api/admin/balance/events", "/api/admin/recharge/orders", "/api/admin/recharge/orders/1/events")) {
            mvc.perform(get(path)).andExpect(status().isUnauthorized());
        }
        mvc.perform(post("/api/admin/accounts/42/balance/credit").contentType("application/json")
                .content("{\"amount_yuan\":1,\"reason\":\"correction\",\"idempotency_key\":\"admin-credit-test-0001\"}"))
                .andExpect(status().isUnauthorized());
        verifyNoInteractions(service);
    }

    @Test void preservesFinancialPageShapeAndQueryFilters() throws Exception {
        when(service.ledger("session", 42L, 2, 4)).thenReturn(new BillingQueryService.LedgerPage(List.of(), 0, 2, 4, 100, null));
        mvc.perform(get("/api/admin/balance/events?account_id=42&limit=2&offset=4").cookie(new Cookie("job_agent_session", "session")))
                .andExpect(status().isOk()).andExpect(jsonPath("$.entries").isArray())
                .andExpect(jsonPath("$.max_pages").value(org.hamcrest.Matchers.nullValue()));
        when(service.orders("session", null, 100, 0)).thenReturn(new BillingQueryService.AdminOrdersPage(List.of(), 0, 100, 0, 100, null));
        mvc.perform(get("/api/admin/recharge/orders").cookie(new Cookie("job_agent_session", "session")))
                .andExpect(status().isOk()).andExpect(jsonPath("$.orders").isArray()).andExpect(jsonPath("$.page_size").value(100));
        verify(service).ledger("session", 42L, 2, 4);
        verify(service).orders("session", null, 100, 0);
    }

    @Test void creditUsesCookieAndPathNotClaimedActor() throws Exception {
        when(service.credit("session", 42L, new BigDecimal("1.000001"), "reason", "admin-credit-test-0001", "trace-test"))
                .thenReturn(new BillingQueryService.CreditResult(null, Map.of("account_id", 42)));
        mvc.perform(post("/api/admin/accounts/42/balance/credit").cookie(new Cookie("job_agent_session", "session"))
                .header("X-Request-ID", "trace-test").contentType("application/json")
                .content("{\"amount_yuan\":\"1.000001\",\"reason\":\"reason\",\"idempotency_key\":\"admin-credit-test-0001\",\"actor_account_id\":99}"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.summary.account_id").value(42));
        verify(service).credit("session", 42L, new BigDecimal("1.000001"), "reason", "admin-credit-test-0001", "trace-test");
    }

    @Test void validatesPageAndMoneyBeforeServiceCalls() throws Exception {
        for (String query : List.of("limit=0", "limit=101", "offset=-1", "offset=oops", "account_id=oops")) {
            mvc.perform(get("/api/admin/balance/events?" + query).cookie(new Cookie("job_agent_session", "session")))
                    .andExpect(status().isUnprocessableEntity()).andExpect(jsonPath("$.detail").isString());
        }
        for (String amount : List.of("0", "-1", "1000001", "0.0000001")) {
            mvc.perform(post("/api/admin/accounts/42/balance/credit").cookie(new Cookie("job_agent_session", "session"))
                    .contentType("application/json").content("{\"amount_yuan\":" + amount + ",\"reason\":\"reason\",\"idempotency_key\":\"admin-credit-test-0001\"}"))
                    .andExpect(status().isUnprocessableEntity());
        }
        verifyNoInteractions(service);
    }

    @Test void internalPagesRequireTokenAndRealSession() throws Exception {
        String body = "{\"session_token\":\"session\",\"account_id\":42,\"limit\":100,\"offset\":0,\"actor_account_id\":99}";
        mvc.perform(post("/internal/v1/billing/admin/orders").contentType("application/json").content(body))
                .andExpect(status().isUnauthorized());
        verifyNoInteractions(service);
        mvc.perform(post("/internal/v1/billing/admin/orders").contentType("application/json").content(body)
                .header("X-Internal-Service-Token", "admin-query-secret")).andExpect(status().isOk());
        verify(service).orders("session", 42L, 100, 0);
    }

    @TestConfiguration static class Tokens {
        @Bean InternalTokenVerifier verifier() { return new InternalTokenVerifier("admin-query-secret"); }
    }
}
