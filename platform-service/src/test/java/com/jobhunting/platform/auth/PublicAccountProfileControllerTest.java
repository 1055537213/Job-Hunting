package com.jobhunting.platform.auth;

import com.jobhunting.platform.security.ClientAddressResolver;
import com.jobhunting.platform.security.RedisRequestLimiter;
import com.jobhunting.platform.security.WebSecurityFilter;
import com.jobhunting.platform.security.WebSecuritySettings;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import jakarta.servlet.http.Cookie;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.test.web.servlet.MockMvc;
import tools.jackson.databind.ObjectMapper;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import static org.springframework.test.web.servlet.setup.MockMvcBuilders.standaloneSetup;

class PublicAccountProfileControllerTest {
    final AccountAdministrationService accounts = mock(AccountAdministrationService.class);
    final AccountActionEmailService emails = mock(AccountActionEmailService.class);
    final RedisRequestLimiter limiter = mock(RedisRequestLimiter.class);
    final Cookie session = new Cookie("job_agent_session", "opaque-session");
    final Cookie csrf = new Cookie("job_agent_csrf", "csrf-secret");
    MockMvc mvc;

    @BeforeEach void setup() {
        when(accounts.current("opaque-session")).thenReturn(new AccountAdministrationService.AccountResponse(account()));
        var security = new WebSecurityFilter(accounts, limiter,
                new WebSecuritySettings(true, "development", true, false, "http://localhost:8000",
                        "test:profile", 60, 20, 120, 120, 240), new ObjectMapper(), new SimpleMeterRegistry(),
                new ClientAddressResolver(""));
        mvc = standaloneSetup(new PublicAccountProfileController(accounts, emails))
                .setControllerAdvice(new PublicAuthErrorHandler()).addFilters(security).build();
    }

    @Test void guestAndMissingCsrfCannotWriteEitherResource() throws Exception {
        for (String resource : new String[] {"profile", "email"}) {
            String body = resource.equals("profile") ? "{\"display_name\":\"Updated\"}" : "{\"new_email\":\"new@example.com\"}";
            mvc.perform(patch("/api/account/" + resource).contentType("application/json").content(body))
                    .andExpect(status().isUnauthorized());
            mvc.perform(patch("/api/account/" + resource).cookie(session).contentType("application/json").content(body))
                    .andExpect(status().isForbidden());
        }
        verify(accounts, never()).updateProfile(any(), any(), any());
        verifyNoInteractions(emails);
    }

    @Test void profileUsesRealSessionAndSafeProjectionAndNormalizedRequestId() throws Exception {
        when(accounts.updateProfile("opaque-session", " Updated ", "profile-trace"))
                .thenReturn(new AccountAdministrationService.AccountResponse(account()));
        mvc.perform(patch("/api/account/profile").cookie(session, csrf).header("X-CSRF-Token", "csrf-secret")
                .header("X-Request-ID", "profile-trace").contentType("application/json")
                .content("{\"display_name\":\" Updated \",\"account_id\":999,\"role\":\"admin\"}"))
                .andExpect(status().isOk()).andExpect(header().string("Cache-Control", "no-store"))
                .andExpect(jsonPath("$.account.id").value(2)).andExpect(jsonPath("$.account.role").value("user"))
                .andExpect(jsonPath("$.account.password_hash").doesNotExist())
                .andExpect(jsonPath("$.account.account_id").doesNotExist());
        verify(accounts).updateProfile("opaque-session", " Updated ", "profile-trace");
    }

    @Test void absentAndNullDisplayNamesCanClearTheValue() throws Exception {
        when(accounts.updateProfile(eq("opaque-session"), isNull(), anyString()))
                .thenReturn(new AccountAdministrationService.AccountResponse(account()));
        for (String body : new String[] {"{}", "{\"display_name\":null}"}) {
            mvc.perform(patch("/api/account/profile").cookie(session, csrf).header("X-CSRF-Token", "csrf-secret")
                    .contentType("application/json").content(body)).andExpect(status().isOk());
        }
        verify(accounts, times(2)).updateProfile(eq("opaque-session"), isNull(), anyString());
    }

    @Test void emailUsesNormalizedTargetAndTrustedAddressNotBodyOrSpoofedForwarding() throws Exception {
        mvc.perform(patch("/api/account/email").cookie(session, csrf).header("X-CSRF-Token", "csrf-secret")
                .header("X-Forwarded-For", "203.0.113.9").contentType("application/json")
                .content("{\"new_email\":\" NEW@Example.com \",\"account_id\":999,\"source\":\"forged\"}"))
                .andExpect(status().isOk()).andExpect(header().string("Cache-Control", "no-store"))
                .andExpect(jsonPath("$.message").value("确认邮件已发送，请打开新邮箱中的链接完成变更。"));
        verify(emails).requestEmailChange("opaque-session", "new@example.com", "127.0.0.1");
    }

    @Test void invalidFieldsAreRejectedBeforeCallingWriteServices() throws Exception {
        mvc.perform(patch("/api/account/profile").cookie(session, csrf).header("X-CSRF-Token", "csrf-secret")
                .contentType("application/json").content("{\"display_name\":\"" + "a".repeat(129) + "\"}"))
                .andExpect(status().isUnprocessableEntity());
        for (String body : new String[] {"{}", "{\"new_email\":null}", "{\"new_email\":\"invalid-email\"}"}) {
            mvc.perform(patch("/api/account/email").cookie(session, csrf).header("X-CSRF-Token", "csrf-secret")
                    .contentType("application/json").content(body)).andExpect(status().isUnprocessableEntity())
                    .andExpect(jsonPath("$.detail").isString());
        }
        verify(accounts, never()).updateProfile(any(), any(), any());
        verifyNoInteractions(emails);
    }

    @Test void conflictsAndUnchangedEmailKeepBusinessStatus() throws Exception {
        for (HttpStatus status : new HttpStatus[] {HttpStatus.CONFLICT, HttpStatus.BAD_REQUEST}) {
            doThrow(new AuthException("EMAIL_REJECTED", "邮箱变更被拒绝。", status))
                    .when(emails).requestEmailChange(anyString(), anyString(), anyString());
            mvc.perform(patch("/api/account/email").cookie(session, csrf).header("X-CSRF-Token", "csrf-secret")
                    .contentType("application/json").content("{\"new_email\":\"new@example.com\"}"))
                    .andExpect(status().is(status.value())).andExpect(jsonPath("$.code").value("EMAIL_REJECTED"));
        }
    }

    @Test void revocationAndRateLimitFailBeforeAnyWrite() throws Exception {
        when(accounts.current("opaque-session")).thenThrow(new AuthException("SESSION_EXPIRED", "expired", HttpStatus.UNAUTHORIZED));
        mvc.perform(patch("/api/account/profile").cookie(session, csrf).header("X-CSRF-Token", "csrf-secret")
                .contentType("application/json").content("{}")).andExpect(status().isUnauthorized());
        when(limiter.check(anyString(), anyString())).thenReturn(9L);
        mvc.perform(patch("/api/account/email").cookie(session, csrf).header("X-CSRF-Token", "csrf-secret")
                .contentType("application/json").content("{}")).andExpect(status().isTooManyRequests())
                .andExpect(header().string("Retry-After", "9"));
        verify(accounts, never()).updateProfile(any(), any(), any());
        verifyNoInteractions(emails);
    }

    @Test void emailPayloadRepresentationsDoNotRevealSessionOrTarget() {
        assertThat(new AccountActionEmailController.EmailRequest(" NEW@Example.com ", "source", "secret-session").email())
                .isEqualTo("new@example.com");
        assertThat(new AccountActionEmailController.EmailRequest("new@example.com", "source", "secret-session").toString())
                .doesNotContain("secret-session", "new@example.com");
        assertThat(new PublicAccountProfileController.EmailChangeRequest("new@example.com").toString())
                .doesNotContain("new@example.com");
    }

    private AccountAdministrationService.AccountView account() {
        return new AccountAdministrationService.AccountView(2L, "owner@example.com", "Updated", "user", "active", false,
                null, null, "2026-10-17T00:00:00Z", "2026-10-17T00:00:00Z");
    }
}
