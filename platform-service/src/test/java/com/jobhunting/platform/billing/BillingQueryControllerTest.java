package com.jobhunting.platform.billing;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import java.util.List;
import java.util.Map;
import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.AuthErrorHandler;
import com.jobhunting.platform.auth.PublicAuthErrorHandler;
import com.jobhunting.platform.security.WebSecurityFilter;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(BillingQueryController.class)
@Import({AuthErrorHandler.class, PublicAuthErrorHandler.class, BillingQueryControllerTest.Tokens.class})
@TestPropertySource(properties = "platform.auth.enabled=true")
class BillingQueryControllerTest {
    @Autowired MockMvc mvc;
    @MockitoBean BillingQueryService queries;
    @MockitoBean AccountAdministrationService accounts;

    @Test
    void guestCannotReadFinancialData() throws Exception {
        mvc.perform(get("/api/me/balance")).andExpect(status().isUnauthorized());
        mvc.perform(get("/api/me/recharge/orders")).andExpect(status().isUnauthorized());
        verifyNoInteractions(queries);
    }

    @Test
    void publicReadsUseTheAuthenticatedOwnerAndKeepThePageShape() throws Exception {
        when(queries.balance(account(), 1, 2)).thenReturn(new BillingQueryService.BalancePage(
                Map.of("account_id", 42L), List.of(), 2, 1, 2, 100, null, Map.of("configured", true)));
        mvc.perform(get("/api/me/balance").param("limit", "1").param("offset", "2").param("account_id", "99")
                .with(request -> { request.setAttribute(WebSecurityFilter.ACCOUNT_ATTRIBUTE, account()); return request; }))
            .andExpect(status().isOk()).andExpect(jsonPath("$.summary.account_id").value(42))
            .andExpect(jsonPath("$.page_size").value(100)).andExpect(jsonPath("$.max_pages").value(org.hamcrest.Matchers.nullValue()));
        when(queries.orders(account(), 100, 0)).thenReturn(new BillingQueryService.OrdersPage(List.of(), 0, 100, 0));
        mvc.perform(get("/api/me/recharge/orders").param("account_id", "99")
                .with(request -> { request.setAttribute(WebSecurityFilter.ACCOUNT_ATTRIBUTE, account()); return request; }))
            .andExpect(status().isOk()).andExpect(jsonPath("$.limit").value(100)).andExpect(jsonPath("$.orders").isArray());
        verify(queries).orders(account(), 100, 0);
    }

    @Test
    void invalidPagesReturnBrowserCompatibleValidationErrors() throws Exception {
        for (String query : List.of("limit=0", "limit=101", "offset=-1", "limit=oops", "offset=9223372036854775808")) {
            mvc.perform(get("/api/me/balance?" + query)
                    .with(request -> { request.setAttribute(WebSecurityFilter.ACCOUNT_ATTRIBUTE, account()); return request; }))
                .andExpect(status().isUnprocessableEntity()).andExpect(jsonPath("$.detail").isString());
        }
        verifyNoInteractions(queries);
    }

    @Test
    void internalAdapterRequiresServiceTokenAndResolvesTheRealSession() throws Exception {
        String body = "{\"session_token\":\"session\",\"limit\":100,\"offset\":0,\"account_id\":99}";
        mvc.perform(post("/internal/v1/billing/me/recharge/orders").contentType("application/json").content(body))
                .andExpect(status().isUnauthorized());
        verifyNoInteractions(accounts, queries);
        when(accounts.current("session")).thenReturn(new AccountAdministrationService.AccountResponse(account()));
        when(queries.orders(account(), 100, 0)).thenReturn(new BillingQueryService.OrdersPage(List.of(), 0, 100, 0));
        mvc.perform(post("/internal/v1/billing/me/recharge/orders").contentType("application/json").content(body)
                .header("X-Internal-Service-Token", "query-secret"))
                .andExpect(status().isOk()).andExpect(jsonPath("$.total").value(0));
        verify(queries).orders(account(), 100, 0);
    }

    private static AccountAdministrationService.AccountView account() {
        return new AccountAdministrationService.AccountView(42L, "query@example.com", null, "user", "active", false,
                null, null, "2026-10-14T00:00:00+00:00", "2026-10-14T00:00:00+00:00");
    }

    @TestConfiguration
    static class Tokens {
        @Bean InternalTokenVerifier tokenVerifier() { return new InternalTokenVerifier("query-secret"); }
    }
}
