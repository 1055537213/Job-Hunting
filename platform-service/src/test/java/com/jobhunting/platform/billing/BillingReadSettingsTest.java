package com.jobhunting.platform.billing;

import java.math.BigDecimal;
import org.junit.jupiter.api.Test;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.context.annotation.Configuration;
import static org.assertj.core.api.Assertions.assertThat;

class BillingReadSettingsTest {
    private final ApplicationContextRunner runner = new ApplicationContextRunner()
            .withUserConfiguration(SettingsConfiguration.class);

    @Test
    void productionDefaultsDisableDemoAndPreserveSharedThreshold() {
        runner.withPropertyValues("platform.billing.environment=production",
                "platform.billing.low-balance-threshold-yuan=12.5",
                "platform.billing.price-per-million-tokens-yuan=30",
                "platform.billing.demo-recharge-enabled=").run(context -> {
            assertThat(context).hasNotFailed();
            var settings = context.getBean(BillingReadSettings.class);
            assertThat(settings.thresholdMicroYuan()).isEqualTo(12_500_000);
            assertThat(settings.publicView()).containsEntry("demo_recharge_enabled", false)
                    .containsEntry("price_per_million_tokens_yuan", new BigDecimal("30"))
                    .containsEntry("starting_balance_yuan", BigDecimal.ZERO);
        });
    }

    @Test
    void explicitDemoSwitchAndLegacyMicroThresholdRemainSupported() {
        runner.withPropertyValues("platform.billing.environment=production",
                "platform.billing.demo-recharge-enabled=true",
                "platform.billing.low-balance-threshold-micro-yuan=12500000",
                "platform.billing.low-balance-threshold-yuan=").run(context -> {
            assertThat(context).hasNotFailed();
            var settings = context.getBean(BillingReadSettings.class);
            assertThat(settings.thresholdMicroYuan()).isEqualTo(12_500_000);
            assertThat(settings.publicView()).containsEntry("demo_recharge_enabled", true);
        });
    }

    @Test
    void invalidMoneyOrCapsPreventStartup() {
        for (String invalid : new String[]{"platform.billing.price-per-million-tokens-yuan=0",
                "platform.billing.low-balance-threshold-yuan=-1",
                "platform.billing.low-balance-threshold-yuan=99999999999999",
                "platform.billing.demo-recharge-max-total-yuan=1"}) {
            runner.withPropertyValues(invalid).run(context -> assertThat(context).hasFailed());
        }
    }

    @Configuration(proxyBeanMethods = false)
    @EnableConfigurationProperties(BillingReadSettings.class)
    static class SettingsConfiguration { }
}
