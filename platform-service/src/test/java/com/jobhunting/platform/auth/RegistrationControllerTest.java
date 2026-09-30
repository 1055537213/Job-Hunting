package com.jobhunting.platform.auth;

import static org.springframework.http.MediaType.APPLICATION_JSON;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.util.List;

import com.jobhunting.platform.billing.InternalTokenVerifier;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.http.HttpStatus;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(RegistrationController.class)
@Import({AuthErrorHandler.class, RegistrationControllerTest.StubConfiguration.class})
@TestPropertySource(properties = "platform.auth.enabled=true")
class RegistrationControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void registersWithTheInternalServiceToken() throws Exception {
        mockMvc.perform(post("/internal/v1/auth/register")
                        .header("X-Internal-Service-Token", "platform-secret")
                        .contentType(APPLICATION_JSON)
                        .content("""
                                {
                                  "email": "user@example.com",
                                  "password": "password-123",
                                  "display_name": "Test User",
                                  "email_verified": false,
                                  "consents": [
                                    {"document_type": "terms", "version": "2026-01"}
                                  ]
                                }
                                """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.account_id").value(43));
    }

    @Test
    void rejectsDuplicateRegistrationWithoutExposingPasswordDetails() throws Exception {
        mockMvc.perform(post("/internal/v1/auth/register")
                        .header("X-Internal-Service-Token", "platform-secret")
                        .contentType(APPLICATION_JSON)
                        .content("""
                                {
                                  "email": "duplicate@example.com",
                                  "password": "password-123"
                                }
                                """))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("ACCOUNT_ALREADY_EXISTS"))
                .andExpect(jsonPath("$.message").value("该邮箱已经注册。"));
    }

    @TestConfiguration
    static class StubConfiguration {
        @Bean
        InternalTokenVerifier internalTokenVerifier() {
            return new InternalTokenVerifier("platform-secret");
        }

        @Bean
        RegistrationService registrationService() {
            return new RegistrationService(null, null, 10_000_000) {
                @Override
                public long register(
                        String email,
                        String password,
                        String displayName,
                        boolean emailVerified,
                        List<RegistrationController.Consent> consents) {
                    if ("duplicate@example.com".equals(email)) {
                        throw new AuthException(
                                "ACCOUNT_ALREADY_EXISTS", "该邮箱已经注册。", HttpStatus.CONFLICT);
                    }
                    return 43L;
                }
            };
        }
    }
}
