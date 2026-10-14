package com.jobhunting.platform.billing;

import java.math.BigDecimal;
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

@WebMvcTest(PersonalRechargeController.class)
@Import({PublicAuthErrorHandler.class, PersonalRechargeControllerTest.Tokens.class})
@TestPropertySource(properties = "platform.auth.enabled=true")
class PersonalRechargeControllerTest {
    @Autowired MockMvc mvc;
    @MockitoBean PersonalRechargeService service;

    @Test void requiresSessionAndIgnoresClaimedAccountAndCaps() throws Exception {
        String body = "{\"amount_yuan\":1.25,\"note\":\"test\",\"idempotency_key\":\"personal-recharge-0001\",\"account_id\":99,\"max_total_micro_yuan\":999999999}";
        mvc.perform(post("/api/me/balance/recharge").contentType("application/json").content(body))
                .andExpect(status().isUnauthorized());
        verifyNoInteractions(service);
        when(service.recharge("session", new BigDecimal("1.25"), "test", "personal-recharge-0001"))
                .thenReturn(new BillingQueryService.RechargeResult(null, null, Map.of("account_id", 42)));
        mvc.perform(post("/api/me/balance/recharge").cookie(new Cookie("job_agent_session", "session"))
                .contentType("application/json").content(body)).andExpect(status().isOk())
                .andExpect(jsonPath("$.summary.account_id").value(42));
        verify(service).recharge("session", new BigDecimal("1.25"), "test", "personal-recharge-0001");
    }

    @Test void validatesMoneyPrecisionAndOptionalKey() throws Exception {
        for (String amount : new String[]{"0", "-1", "1000001", "0.0000001", "null"}) {
            mvc.perform(post("/api/me/balance/recharge").cookie(new Cookie("job_agent_session", "session"))
                    .contentType("application/json").content("{\"amount_yuan\":" + amount + "}"))
                    .andExpect(status().isUnprocessableEntity()).andExpect(jsonPath("$.detail").isString());
        }
        verifyNoInteractions(service);
        mvc.perform(post("/api/me/balance/recharge").cookie(new Cookie("job_agent_session", "session"))
                .contentType("application/json").content("{\"amount_yuan\":1}"))
                .andExpect(status().isOk());
        verify(service).recharge("session", BigDecimal.ONE, null, null);
    }

    @Test void internalAdapterNeedsServiceTokenAndOriginalSession() throws Exception {
        String body = "{\"session_token\":\"session\",\"payload\":{\"amount_yuan\":1}}";
        mvc.perform(post("/internal/v1/billing/me/recharge").contentType("application/json").content(body))
                .andExpect(status().isUnauthorized());
        verifyNoInteractions(service);
        mvc.perform(post("/internal/v1/billing/me/recharge").header("X-Internal-Service-Token", "recharge-secret")
                .contentType("application/json").content(body)).andExpect(status().isOk());
        verify(service).recharge("session", BigDecimal.ONE, null, null);
    }

    @TestConfiguration static class Tokens {
        @Bean InternalTokenVerifier verifier() { return new InternalTokenVerifier("recharge-secret"); }
    }
}
