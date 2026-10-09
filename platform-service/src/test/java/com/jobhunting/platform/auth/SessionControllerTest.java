package com.jobhunting.platform.auth;

import static org.mockito.Mockito.*;
import static org.springframework.http.MediaType.APPLICATION_JSON;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import com.jobhunting.platform.billing.InternalTokenVerifier;
import com.jobhunting.platform.billing.BillingErrorHandler;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(SessionController.class)
@Import({AuthErrorHandler.class, BillingErrorHandler.class, SessionControllerTest.Configuration.class})
@TestPropertySource(properties = "platform.auth.enabled=true")
class SessionControllerTest {
    @Autowired MockMvc mvc;
    @MockitoBean SessionService service;

    @Test void everyOperationRequiresInternalToken() throws Exception {
        for (String operation : new String[] {"login", "resolve", "logout", "logout-all", "change-password"}) {
            String body = switch (operation) {
                case "login" -> "{\"email\":\"a@example.com\",\"password\":\"password-123\",\"email_verification_required\":true}";
                case "change-password" -> "{\"session_token\":\"opaque\",\"current_password\":\"password-123\",\"new_password\":\"new-password\"}";
                default -> "{\"session_token\":\"opaque\"}";
            };
            mvc.perform(post("/internal/v1/auth/sessions/" + operation).contentType(APPLICATION_JSON).content(body))
                .andExpect(status().isUnauthorized());
        }
        verifyNoInteractions(service);
    }

    @Test void resolveAndLoginUseExplicitTokenContract() throws Exception {
        when(service.resolve("opaque")).thenReturn(null);
        mvc.perform(post("/internal/v1/auth/sessions/resolve").header("X-Internal-Service-Token", "test-token")
            .contentType(APPLICATION_JSON).content("{\"session_token\":\"opaque\"}"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.account_id").isEmpty());
        when(service.login("a@example.com", "password-123", false, null, null)).thenReturn(new SessionService.Login(1, "opaque-result"));
        mvc.perform(post("/internal/v1/auth/sessions/login").header("X-Internal-Service-Token", "test-token")
            .contentType(APPLICATION_JSON).content("{\"email\":\"a@example.com\",\"password\":\"password-123\",\"email_verification_required\":false}"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.session_token").value("opaque-result"));
    }

    @Test void passwordLengthIsValidatedAndSecretsAreRedacted() throws Exception {
        mvc.perform(post("/internal/v1/auth/sessions/change-password").header("X-Internal-Service-Token", "test-token")
            .contentType(APPLICATION_JSON).content("{\"session_token\":\"opaque\",\"current_password\":\"password-123\",\"new_password\":\"short\"}"))
            .andExpect(status().isUnprocessableEntity());
        verifyNoInteractions(service);
        org.assertj.core.api.Assertions.assertThat(new SessionController.PasswordRequest("opaque", "old-password", "new-password").toString())
            .doesNotContain("opaque", "old-password", "new-password");
    }
    @TestConfiguration static class Configuration {
        @Bean InternalTokenVerifier tokens() { return new InternalTokenVerifier("test-token"); }
    }
}
