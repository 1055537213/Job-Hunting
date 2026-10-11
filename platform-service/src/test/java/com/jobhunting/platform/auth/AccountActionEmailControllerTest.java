package com.jobhunting.platform.auth;

import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.http.MediaType.APPLICATION_JSON;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.jobhunting.platform.billing.InternalTokenVerifier;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(AccountActionEmailController.class)
@Import({AuthErrorHandler.class, AccountActionEmailControllerTest.Configuration.class})
@TestPropertySource(properties = "platform.auth.enabled=true")
class AccountActionEmailControllerTest {
    @Autowired private MockMvc mvc;
    @MockitoBean private AccountActionEmailService service;

    @Test
    void allOperationsRequireInternalAuthentication() throws Exception {
        String[][] requests = {
            {"request", "{\"email\":\"user@example.com\"}"},
            {"confirm", "{\"token\":\"test-token\",\"new_password\":\"new-password-123\"}"},
            {"due", "{}"}, {"claim", "{\"id\":1}"},
            {"finish", "{\"id\":1,\"claim_key\":\"key\",\"sent\":true}"},
            {"observations", "{}"}
        };
        for (var request : requests) {
          for (String action : new String[] {"email-verification", "password-reset", "email-change"}) {
            for (String token : new String[] {"", "wrong-token"}) {
                mvc.perform(post("/internal/v1/auth/" + action + "/" + request[0])
                        .contentType(APPLICATION_JSON).content(request[1])
                        .header("X-Internal-Service-Token", token))
                    .andExpect(status().isUnauthorized());
            }
          }
        }
        verifyNoInteractions(service);
    }

    @Test
    void claimAndFinishUseFencingKey() throws Exception {
        when(service.claim(AccountActionEmailService.Purpose.VERIFY_EMAIL, 1)).thenReturn(null);
        mvc.perform(post("/internal/v1/auth/email-verification/claim")
                .header("X-Internal-Service-Token", "test-internal-token")
                .contentType(APPLICATION_JSON).content("{\"id\":1}"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.claim").isEmpty());
        when(service.finish(AccountActionEmailService.Purpose.VERIFY_EMAIL, 1, "claim-key", true, null)).thenReturn(true);
        mvc.perform(post("/internal/v1/auth/email-verification/finish")
                .header("X-Internal-Service-Token", "test-internal-token")
                .contentType(APPLICATION_JSON)
                .content("{\"id\":1,\"claim_key\":\"claim-key\",\"sent\":true}"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.ok").value(true));
        verify(service).finish(AccountActionEmailService.Purpose.VERIFY_EMAIL, 1, "claim-key", true, null);
    }

    @Test
    void rejectsInvalidDeliveryResults() throws Exception {
        mvc.perform(post("/internal/v1/auth/email-verification/finish")
                .header("X-Internal-Service-Token", "test-internal-token")
                .contentType(APPLICATION_JSON)
                .content("{\"id\":1,\"claim_key\":\"key\",\"sent\":false,\"error_type\":\"secret=https://token\"}"))
            .andExpect(status().isUnprocessableEntity());
        mvc.perform(post("/internal/v1/auth/email-verification/finish")
                .header("X-Internal-Service-Token", "test-internal-token")
                .contentType(APPLICATION_JSON)
                .content("{\"id\":1,\"claim_key\":\"key\"}"))
            .andExpect(status().isUnprocessableEntity());
        verifyNoInteractions(service);
    }

    @Test
    void resetUsesValidatedRedactedCredentialsAndPurposeScopedClaim() throws Exception {
        when(service.resetPassword("opaque-token", "new-password-123")).thenReturn(1L);
        mvc.perform(post("/internal/v1/auth/password-reset/confirm")
                .header("X-Internal-Service-Token", "test-internal-token")
                .contentType(APPLICATION_JSON)
                .content("{\"token\":\"opaque-token\",\"new_password\":\"new-password-123\"}"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.account_id").value(1));
        verify(service).resetPassword("opaque-token", "new-password-123");
        when(service.claim(AccountActionEmailService.Purpose.RESET_PASSWORD, 1)).thenReturn(null);
        mvc.perform(post("/internal/v1/auth/password-reset/claim")
                .header("X-Internal-Service-Token", "test-internal-token")
                .contentType(APPLICATION_JSON).content("{\"id\":1}"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.claim").isEmpty());
        verify(service).claim(AccountActionEmailService.Purpose.RESET_PASSWORD, 1);
        org.assertj.core.api.Assertions.assertThat(new AccountActionEmailController.TokenRequest("opaque-token", "new-password-123").toString())
            .doesNotContain("opaque-token", "new-password-123");
    }

    @Test
    void emailChangeRequiresSessionForRequestAndReturnsAccountIdOnConfirmation() throws Exception {
        mvc.perform(post("/internal/v1/auth/email-change/request")
                .header("X-Internal-Service-Token", "test-internal-token")
                .contentType(APPLICATION_JSON)
                .content("{\"email\":\"new@example.com\",\"source\":\"source\"}"))
            .andExpect(status().isUnauthorized());
        when(service.changeEmail("opaque-token")).thenReturn(7L);
        mvc.perform(post("/internal/v1/auth/email-change/confirm")
                .header("X-Internal-Service-Token", "test-internal-token")
                .contentType(APPLICATION_JSON)
                .content("{\"token\":\"opaque-token\"}"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.account_id").value(7));
        verify(service).changeEmail("opaque-token");
    }

    @Test
    void rejectsShortResetPasswordBeforeServiceCall() throws Exception {
        mvc.perform(post("/internal/v1/auth/password-reset/confirm")
                .header("X-Internal-Service-Token", "test-internal-token")
                .contentType(APPLICATION_JSON).content("{\"token\":\"opaque\",\"new_password\":\"short\"}"))
            .andExpect(status().isUnprocessableEntity());
        verifyNoInteractions(service);
    }

    @TestConfiguration
    static class Configuration {
        @Bean InternalTokenVerifier verifier() { return new InternalTokenVerifier("test-internal-token"); }
    }
}
