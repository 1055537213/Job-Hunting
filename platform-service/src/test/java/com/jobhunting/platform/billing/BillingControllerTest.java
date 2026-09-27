package com.jobhunting.platform.billing;

import static org.springframework.http.MediaType.APPLICATION_JSON;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.http.HttpStatus;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(BillingController.class)
@Import({BillingErrorHandler.class, BillingControllerTest.StubBillingConfiguration.class})
@TestPropertySource(properties = "platform.billing.enabled=true")
class BillingControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void consumeReturnsTheChargeResultAndAcceptsTheInternalToken() throws Exception {
        mockMvc.perform(post("/internal/v1/billing/consume")
                        .header("Idempotency-Key", "call-20260927-0001")
                        .header("X-Internal-Service-Token", "platform-secret")
                        .header("X-Trace-Id", "trace-42")
                        .contentType(APPLICATION_JSON)
                        .content("""
                                {
                                  "account_id": 42,
                                  "amount_micro_yuan": 2000000,
                                  "token_count": 80000,
                                  "source_reference": "call-20260927-0001",
                                  "description": "模型调用扣费"
                                }
                                """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.account_id").value(42))
                .andExpect(jsonPath("$.balance_micro_yuan").value(8_000_000))
                .andExpect(jsonPath("$.ledger_entry_id").value(7))
                .andExpect(jsonPath("$.replayed").value(false));
    }

    @Test
    void insufficientBalanceUsesTheUserFacingBusinessError() throws Exception {
        mockMvc.perform(post("/internal/v1/billing/consume")
                        .header("Idempotency-Key", "call-20260927-0002")
                        .header("X-Internal-Service-Token", "platform-secret")
                        .header("X-Trace-Id", "trace-43")
                        .contentType(APPLICATION_JSON)
                        .content("""
                                {
                                  "account_id": 42,
                                  "amount_micro_yuan": 2000000,
                                  "source_reference": "call-20260927-0002"
                                }
                                """))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("INSUFFICIENT_BALANCE"))
                .andExpect(jsonPath("$.message").value("余额不足，请先充值后重试"))
                .andExpect(jsonPath("$.trace_id").value("trace-43"));
    }

    @Test
    void invalidInternalTokenIsRejected() throws Exception {
        mockMvc.perform(post("/internal/v1/billing/consume")
                        .header("Idempotency-Key", "call-20260927-0001")
                        .header("X-Internal-Service-Token", "wrong-token")
                        .header("X-Trace-Id", "trace-44")
                        .contentType(APPLICATION_JSON)
                        .content("""
                                {
                                  "account_id": 42,
                                  "amount_micro_yuan": 2000000,
                                  "source_reference": "call-20260927-0001"
                                }
                                """))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value("INTERNAL_UNAUTHORIZED"))
                .andExpect(jsonPath("$.trace_id").value("trace-44"));
    }

    @Test
    void rechargeReturnsTheUpdatedBalanceAndAcceptsTheInternalToken() throws Exception {
        mockMvc.perform(post("/internal/v1/billing/recharge")
                        .header("Idempotency-Key", "recharge-20260927-0001")
                        .header("X-Internal-Service-Token", "platform-secret")
                        .header("X-Trace-Id", "trace-recharge-42")
                        .contentType(APPLICATION_JSON)
                        .content("""
                                {
                                  "account_id": 42,
                                  "amount_micro_yuan": 10000000,
                                  "source_reference": "recharge-20260927-0001",
                                  "actor_account_id": 42,
                                  "description": "个人中心模拟充值"
                                }
                                """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.account_id").value(42))
                .andExpect(jsonPath("$.balance_micro_yuan").value(18_000_000))
                .andExpect(jsonPath("$.total_recharge_micro_yuan").value(20_000_000));
    }

    @TestConfiguration
    static class StubBillingConfiguration {

        @Bean
        BillingOperations billingOperations() {
            return new StubBillingOperations();
        }

        @Bean
        InternalTokenVerifier internalTokenVerifier() {
            return new InternalTokenVerifier("platform-secret");
        }
    }

    static class StubBillingOperations implements BillingOperations {

        @Override
        public BillingDtos.BalanceProjection getBalance(long accountId) {
            return new BillingDtos.BalanceProjection(accountId, 8_000_000, 10_000_000, 2_000_000, 2);
        }

        @Override
        public BillingDtos.ChargeResponse consume(
                BillingDtos.ConsumeRequest request,
                String idempotencyKey) {
            if ("call-20260927-0002".equals(idempotencyKey)) {
                throw new BillingException(
                        "INSUFFICIENT_BALANCE",
                        "余额不足，请先充值后重试",
                        HttpStatus.CONFLICT);
            }
            return new BillingDtos.ChargeResponse(request.account_id(), 8_000_000, 7, false, 2_000_000);
        }

        @Override
        public BillingDtos.BalanceProjection recharge(
                BillingDtos.RechargeRequest request,
                String idempotencyKey) {
            return new BillingDtos.BalanceProjection(request.account_id(), 18_000_000, 20_000_000, 2_000_000, 3);
        }
    }
}
