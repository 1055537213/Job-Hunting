package com.jobhunting.platform.auth;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.*;
import static org.springframework.http.MediaType.APPLICATION_JSON;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import java.util.List;

import com.jobhunting.platform.billing.BillingErrorHandler;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import org.junit.jupiter.api.Test;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.beans.factory.annotation.Autowired;

@WebMvcTest(AccountAdministrationController.class)
@Import({AuthErrorHandler.class, BillingErrorHandler.class, AccountAdministrationControllerTest.Configuration.class})
@TestPropertySource(properties = "platform.auth.enabled=true")
class AccountAdministrationControllerTest {
    @Autowired MockMvc mvc;
    @MockitoBean AccountAdministrationService service;

    @Test
    void everyOperationRequiresInternalToken() throws Exception {
        mvc.perform(post("/internal/v1/auth/accounts/list")
                .contentType(APPLICATION_JSON)
                .content("{\"session_token\":\"opaque\"}"))
            .andExpect(status().isUnauthorized());
        mvc.perform(post("/internal/v1/auth/accounts/status")
                .contentType(APPLICATION_JSON)
                .content("{\"session_token\":\"opaque\",\"account_id\":2,\"status\":\"disabled\"}"))
            .andExpect(status().isUnauthorized());
        mvc.perform(post("/internal/v1/auth/accounts/bootstrap")
                .contentType(APPLICATION_JSON)
                .content("{\"email\":\"admin@example.com\",\"password\":\"password-123\"}"))
            .andExpect(status().isUnauthorized());
        verifyNoInteractions(service);
    }

    @Test
    void forwardsSafeAccountOperationsAndRedactsSecrets() throws Exception {
        var account = new AccountAdministrationService.AccountView(
                2, "user@example.com", "User", "user", "active", false,
                null, null, "2026-10-10T00:00:00Z", "2026-10-10T00:00:00Z");
        when(service.list("opaque")).thenReturn(new AccountAdministrationService.AccountList(List.of(account)));
        when(service.updateStatus("opaque", 2, "disabled", "trace-2"))
            .thenReturn(new AccountAdministrationService.AccountResponse(account));
        when(service.bootstrap("admin@example.com", "password-123", null))
            .thenReturn(new AccountAdministrationService.BootstrapResult(true, 3L));

        mvc.perform(post("/internal/v1/auth/accounts/list")
                .header("X-Internal-Service-Token", "test-token")
                .contentType(APPLICATION_JSON)
                .content("{\"session_token\":\"opaque\"}"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.accounts[0].account_id").value(2));
        mvc.perform(post("/internal/v1/auth/accounts/status")
                .header("X-Internal-Service-Token", "test-token")
                .header("X-Trace-Id", "trace-2")
                .contentType(APPLICATION_JSON)
                .content("{\"session_token\":\"opaque\",\"account_id\":2,\"status\":\"disabled\"}"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.account.status").value("active"));
        mvc.perform(post("/internal/v1/auth/accounts/bootstrap")
                .header("X-Internal-Service-Token", "test-token")
                .contentType(APPLICATION_JSON)
                .content("{\"email\":\"admin@example.com\",\"password\":\"password-123\"}"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.created").value(true));

        verify(service).list("opaque");
        verify(service).updateStatus("opaque", 2, "disabled", "trace-2");
        verify(service).bootstrap("admin@example.com", "password-123", null);
        assertThat(new AccountAdministrationController.SessionRequest("opaque").toString())
            .doesNotContain("opaque");
        assertThat(new AccountAdministrationController.BootstrapRequest(
                "admin@example.com", "password-123", null).toString())
            .doesNotContain("admin@example.com", "password-123");
    }

    @Test
    void validatesStatusRequestBeforeCallingService() throws Exception {
        mvc.perform(post("/internal/v1/auth/accounts/status")
                .header("X-Internal-Service-Token", "test-token")
                .contentType(APPLICATION_JSON)
                .content("{\"session_token\":\"opaque\",\"account_id\":0,\"status\":\"disabled\"}"))
            .andExpect(status().isUnprocessableEntity());
        verifyNoInteractions(service);
    }

    @TestConfiguration
    static class Configuration {
        @Bean InternalTokenVerifier tokens() { return new InternalTokenVerifier("test-token"); }
    }
}
