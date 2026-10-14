package com.jobhunting.platform.auth;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.http.MediaType.APPLICATION_JSON;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.context.annotation.Import;
import com.jobhunting.platform.billing.BillingQueryService;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import com.jobhunting.platform.security.WebSecurityFilter;

@WebMvcTest(PublicAuthLifecycleController.class)
@Import({AuthErrorHandler.class, PublicAuthErrorHandler.class})
@TestPropertySource(properties = {
    "platform.auth.enabled=true", "JOB_AGENT_CONSENT_REQUIRED=true",
    "JOB_AGENT_TERMS_VERSION=terms-v5", "JOB_AGENT_PRIVACY_VERSION=privacy-v8",
    "JOB_AGENT_EMAIL_VERIFICATION_REQUIRED=true"
})
class PublicAuthLifecycleControllerTest {
    @Autowired MockMvc mvc;
    @MockitoBean RegistrationService registrations;
    @MockitoBean AccountActionEmailService emails;
    @MockitoBean AccountAdministrationService accounts;
    @MockitoBean BillingQueryService billing;

    @Test
    void registrationUsesConfiguredConsentVersionsAndRealSource() throws Exception {
        mvc.perform(get("/api/auth/config"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.consent_required").value(true))
            .andExpect(jsonPath("$.terms_version").value("terms-v5"));
        mvc.perform(post("/api/auth/register").contentType(APPLICATION_JSON)
                .content("{\"email\":\"new@example.com\",\"password\":\"password-123\",\"accepted_terms_version\":\"old\",\"accepted_privacy_version\":\"privacy-v8\"}"))
            .andExpect(status().isBadRequest());
        verifyNoInteractions(registrations);
        when(registrations.register(eq("new@example.com"), eq("password-123"), isNull(), eq(false), anyList()))
            .thenReturn(2L);
        when(accounts.accountById(2L)).thenReturn(account());
        mvc.perform(post("/api/auth/register").contentType(APPLICATION_JSON)
                .header("User-Agent", "browser")
                .content("{\"email\":\"new@example.com\",\"password\":\"password-123\",\"accepted_terms_version\":\"terms-v5\",\"accepted_privacy_version\":\"privacy-v8\"}"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.account.id").value(2))
            .andExpect(jsonPath("$.verification_required").value(true));
        verify(registrations).register(eq("new@example.com"), eq("password-123"), isNull(), eq(false),
            eq(List.of(new RegistrationController.Consent("terms", "terms-v5", "127.0.0.1", "browser"),
                       new RegistrationController.Consent("privacy", "privacy-v8", "127.0.0.1", "browser"))));
    }

    @Test
    void meReturnsAnonymousShapeOrAuthenticatedBalanceWithoutQueryingForGuest() throws Exception {
        mvc.perform(get("/api/auth/me"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.authenticated").value(false))
            .andExpect(jsonPath("$.billing").value(org.hamcrest.Matchers.nullValue()));
        verifyNoInteractions(billing);
        when(billing.summary(account())).thenReturn(Map.of("account_id", 2L,
                "balance_micro_yuan", 5_000_000L, "low_balance_threshold_micro_yuan", 10_000_000L, "state", "low_balance"));
        mvc.perform(get("/api/auth/me").with(request -> {
                request.setAttribute(WebSecurityFilter.ACCOUNT_ATTRIBUTE, account());
                return request;
            }))
            .andExpect(status().isOk()).andExpect(jsonPath("$.authenticated").value(true))
            .andExpect(jsonPath("$.account.id").value(2))
            .andExpect(jsonPath("$.billing.state").value("low_balance"))
            .andExpect(jsonPath("$.csrf_token").isString())
            .andExpect(header().string("Set-Cookie", org.hamcrest.Matchers.containsString("job_agent_csrf=")));
    }

    @Test
    void accountActionRequestsKeepExistencePrivateAndConfirmationUsesJavaOwner() throws Exception {
        mvc.perform(post("/api/auth/password-reset/request").contentType(APPLICATION_JSON)
                .content("{\"email\":\"missing@example.com\"}"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.message").value("如果该邮箱已注册，密码重置邮件已发送。"));
        verify(emails).request(AccountActionEmailService.Purpose.RESET_PASSWORD, "missing@example.com", "127.0.0.1");
        when(emails.verify(anyString())).thenReturn(2L);
        when(accounts.accountById(2L)).thenReturn(account());
        mvc.perform(post("/api/auth/verify-email").contentType(APPLICATION_JSON)
                .content("{\"token\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"}"))
            .andExpect(status().isOk()).andExpect(jsonPath("$.account.email").value("new@example.com"));
    }

    private static AccountAdministrationService.AccountView account() {
        return new AccountAdministrationService.AccountView(2L, "new@example.com", null, "user", "active", false,
                null, null, "2026-10-11T00:00:00Z", "2026-10-11T00:00:00Z");
    }
}
