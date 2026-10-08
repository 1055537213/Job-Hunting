package com.jobhunting.platform.auth;

import static org.springframework.http.MediaType.APPLICATION_JSON;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.http.HttpStatus;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.beans.factory.annotation.Autowired;

@WebMvcTest(CredentialController.class)
@Import({
        AuthErrorHandler.class,
        CredentialControllerTest.StubConfiguration.class
})
@TestPropertySource(properties = "platform.auth.enabled=true")
class CredentialControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void verifiesCredentialsWithTheInternalServiceToken() throws Exception {
        mockMvc.perform(post("/internal/v1/auth/verify-credentials")
                        .header("X-Internal-Service-Token", "platform-secret")
                        .header("X-Trace-Id", "auth-trace-42")
                        .contentType(APPLICATION_JSON)
                        .content("""
                                {
                                  "email": "user@example.com",
                                  "password": "password-123",
                                  "email_verification_required": false
                                }
                                """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.account_id").value(42));
    }

    @Test
    void rejectsInvalidCredentialsWithoutExposingPasswordDetails() throws Exception {
        mockMvc.perform(post("/internal/v1/auth/verify-credentials")
                        .header("X-Internal-Service-Token", "platform-secret")
                        .header("X-Trace-Id", "auth-trace-43")
                        .contentType(APPLICATION_JSON)
                        .content("""
                                {
                                  "email": "missing@example.com",
                                  "password": "wrong-password",
                                  "email_verification_required": false
                                }
                                """))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value("INVALID_CREDENTIALS"))
                .andExpect(jsonPath("$.message").value("邮箱或密码错误。"))
                .andExpect(jsonPath("$.trace_id").value("auth-trace-43"));
    }

    @Test
    void rejectsWrongInternalToken() throws Exception {
        mockMvc.perform(post("/internal/v1/auth/verify-credentials")
                        .header("X-Internal-Service-Token", "wrong-token")
                        .header("X-Trace-Id", "auth-trace-44")
                        .contentType(APPLICATION_JSON)
                        .content("""
                                {
                                  "email": "user@example.com",
                                  "password": "password-123",
                                  "email_verification_required": false
                                }
                                """))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value("INTERNAL_UNAUTHORIZED"));
    }

    @TestConfiguration
    static class StubConfiguration {
        @Bean
        com.jobhunting.platform.billing.InternalTokenVerifier internalTokenVerifier() {
            return new com.jobhunting.platform.billing.InternalTokenVerifier("platform-secret");
        }

        @Bean
        CredentialVerifier credentialVerifier() {
            return (email, password, verificationRequired) -> {
                if ("missing@example.com".equals(email)) {
                    throw new AuthException(
                            "INVALID_CREDENTIALS", "邮箱或密码错误。", HttpStatus.UNAUTHORIZED);
                }
                return 42L;
            };
        }
    }
}
