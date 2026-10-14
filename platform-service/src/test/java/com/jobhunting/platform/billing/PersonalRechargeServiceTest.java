package com.jobhunting.platform.billing;

import java.math.BigDecimal;
import com.jobhunting.platform.auth.AccountAdministrationService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.ObjectProvider;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

class PersonalRechargeServiceTest {
    @Test void disabledDemoAndMissingWriterFailClosed() {
        var accounts = mock(AccountAdministrationService.class);
        var queries = mock(BillingQueryService.class);
        @SuppressWarnings("unchecked") ObjectProvider<BillingService> writer = mock(ObjectProvider.class);
        when(accounts.current("session")).thenReturn(new AccountAdministrationService.AccountResponse(
                new AccountAdministrationService.AccountView(42L, "user@example.com", null, "user", "active", false,
                        null, null, "2026-10-14T00:00:00Z", "2026-10-14T00:00:00Z")));
        var disabled = new BillingReadSettings(new BigDecimal("25"), BigDecimal.ZERO, null, 10000000,
                false, new BigDecimal("20"), new BigDecimal("50"), "production");
        assertThatThrownBy(() -> new PersonalRechargeService(accounts, queries, disabled, writer)
                .recharge("session", BigDecimal.ONE, null, null)).isInstanceOf(BillingException.class)
                .extracting("code").isEqualTo("DEMO_RECHARGE_DISABLED");
        verifyNoInteractions(writer, queries);
        var enabled = new BillingReadSettings(new BigDecimal("25"), BigDecimal.ZERO, null, 10000000,
                true, new BigDecimal("20"), new BigDecimal("50"), "production");
        assertThatThrownBy(() -> new PersonalRechargeService(accounts, queries, enabled, writer)
                .recharge("session", BigDecimal.ONE, null, null)).isInstanceOf(BillingException.class)
                .extracting("code").isEqualTo("BILLING_UNAVAILABLE");
        verifyNoInteractions(queries);
    }
}
