package com.jobhunting.platform.auth;

import static org.mockito.Mockito.*;
import static org.springframework.http.MediaType.APPLICATION_JSON;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(PublicAuthController.class)
@Import({AuthErrorHandler.class, PublicAuthErrorHandler.class})
@TestPropertySource(properties = {
    "platform.auth.enabled=true",
    "JOB_AGENT_CSRF_ENABLED=true",
    "JOB_AGENT_COOKIE_SECURE=false"
})
class PublicAuthControllerTest {
    @Autowired MockMvc mvc;
    @MockitoBean SessionService sessions;
    @MockitoBean AccountAdministrationService accounts;

    @Test
    void loginReturnsCompatibleAccountAndBrowserCookies() throws Exception {
        when(sessions.login("user@example.com", "password-123", false, "browser", "127.0.0.1"))
            .thenReturn(new SessionService.Login(2, "session-token"));
        when(accounts.current("session-token")).thenReturn(new AccountAdministrationService.AccountResponse(account()));

        mvc.perform(post("/api/auth/login")
                .contentType(APPLICATION_JSON)
                .header("User-Agent", "browser")
                .content("{\"email\":\" User@Example.com \",\"password\":\"password-123\"}"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.account.id").value(2))
            .andExpect(jsonPath("$.account.email").value("user@example.com"))
            .andExpect(jsonPath("$.csrf_token").isString())
            .andExpect(header().string("Set-Cookie", org.hamcrest.Matchers.containsString("job_agent_session=")));
    }

    @Test
    void productionCookiesAreSecureAndSessionIsHttpOnly() {
        when(sessions.login("user@example.com", "password-123", true, null, "127.0.0.1"))
            .thenReturn(new SessionService.Login(2, "session-token"));
        when(accounts.current("session-token")).thenReturn(new AccountAdministrationService.AccountResponse(account()));
        var controller = new PublicAuthController(sessions, accounts, true, true, true);
        var request = new org.springframework.mock.web.MockHttpServletRequest();
        request.setRemoteAddr("127.0.0.1");
        var response = new org.springframework.mock.web.MockHttpServletResponse();
        controller.login(new PublicAuthController.LoginRequest("user@example.com", "password-123"), request, response);
        org.assertj.core.api.Assertions.assertThat(response.getHeaders("Set-Cookie"))
            .allSatisfy(cookie -> org.assertj.core.api.Assertions.assertThat(cookie).contains("Secure", "SameSite=Lax", "Path=/"));
        org.assertj.core.api.Assertions.assertThat(response.getHeaders("Set-Cookie").getFirst()).contains("HttpOnly");
    }

    @Test
    void validationNeverIncludesCredentialsOrRawRejectedValues() throws Exception {
        mvc.perform(post("/api/auth/login").contentType(APPLICATION_JSON)
                .content("{\"email\":\"secret@example.com\"}"))
            .andExpect(status().isUnprocessableEntity())
            .andExpect(jsonPath("$.code").value("INVALID_REQUEST"))
            .andExpect(jsonPath("$.detail").isString())
            .andExpect(content().string(org.hamcrest.Matchers.not(org.hamcrest.Matchers.containsString("secret@example.com"))));
        mvc.perform(post("/api/auth/login").contentType(APPLICATION_JSON).content("broken json"))
            .andExpect(status().isBadRequest()).andExpect(jsonPath("$.detail").isString());
        verifyNoInteractions(sessions);
    }

    @Test
    void stateChangingEndpointsRequireCsrfAndClearCookies() throws Exception {
        when(sessions.logoutAll("session-token", null)).thenReturn(1);
        mvc.perform(post("/api/auth/logout-all")
                .cookie(new jakarta.servlet.http.Cookie("job_agent_session", "session-token"),
                        new jakarta.servlet.http.Cookie("job_agent_csrf", "csrf"))
                .header("X-CSRF-Token", "csrf"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.revoked_sessions").value(1))
            .andExpect(header().string("Set-Cookie", org.hamcrest.Matchers.containsString("Max-Age=0")));

        mvc.perform(post("/api/auth/logout-all")
                .cookie(new jakarta.servlet.http.Cookie("job_agent_session", "session-token"),
                        new jakarta.servlet.http.Cookie("job_agent_csrf", "csrf")))
            .andExpect(status().isForbidden());
        verify(sessions, times(1)).logoutAll("session-token", null);
    }

    private static AccountAdministrationService.AccountView account() {
        return new AccountAdministrationService.AccountView(
                2, "user@example.com", null, "user", "active", false,
                null, null, "2026-10-11T00:00:00Z", "2026-10-11T00:00:00Z");
    }

}
