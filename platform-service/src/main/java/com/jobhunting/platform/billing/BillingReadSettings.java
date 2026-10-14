package com.jobhunting.platform.billing;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.Map;
import java.util.Set;
import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.Min;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.context.properties.bind.DefaultValue;
import org.springframework.validation.annotation.Validated;

@Validated
@ConfigurationProperties("platform.billing")
public record BillingReadSettings(
        @DefaultValue("25") @DecimalMin(value = "0", inclusive = false) BigDecimal pricePerMillionTokensYuan,
        @DefaultValue("0") @DecimalMin("0") BigDecimal startingBalanceYuan,
        @DecimalMin(value = "0", inclusive = false) BigDecimal lowBalanceThresholdYuan,
        @DefaultValue("10000000") @Min(1) long lowBalanceThresholdMicroYuan,
        Boolean demoRechargeEnabled,
        @DefaultValue("20") @DecimalMin(value = "0", inclusive = false) BigDecimal demoRechargeMaxAmountYuan,
        @DefaultValue("50") @DecimalMin(value = "0", inclusive = false) BigDecimal demoRechargeMaxTotalYuan,
        @DefaultValue("development") String environment) {

    public BillingReadSettings {
        if (!Set.of("development", "test", "production").contains(environment)
                || demoRechargeMaxTotalYuan.compareTo(demoRechargeMaxAmountYuan) < 0) {
            throw new IllegalArgumentException("Invalid billing environment or demo recharge limits");
        }
        if (lowBalanceThresholdYuan != null) {
            lowBalanceThresholdYuan.movePointRight(6).setScale(0, RoundingMode.HALF_EVEN).longValueExact();
        }
        for (BigDecimal limit : new BigDecimal[]{demoRechargeMaxAmountYuan, demoRechargeMaxTotalYuan}) {
            if (limit.movePointRight(6).setScale(0, RoundingMode.DOWN).longValueExact() < 1) {
                throw new IllegalArgumentException("Demo recharge limits must be at least one micro yuan");
            }
        }
    }

    public long thresholdMicroYuan() {
        return lowBalanceThresholdYuan == null ? lowBalanceThresholdMicroYuan
                : lowBalanceThresholdYuan.movePointRight(6).setScale(0, RoundingMode.HALF_EVEN).longValueExact();
    }

    public Map<String, Object> publicView() {
        return Map.of("configured", true, "price_per_million_tokens_yuan", pricePerMillionTokensYuan,
                "starting_balance_yuan", startingBalanceYuan,
                "low_balance_threshold_yuan", BigDecimal.valueOf(thresholdMicroYuan(), 6),
                "demo_recharge_enabled", demoEnabled(),
                "demo_recharge_max_amount_yuan", demoRechargeMaxAmountYuan,
                "demo_recharge_max_total_yuan", demoRechargeMaxTotalYuan);
    }

    public boolean demoEnabled() {
        return demoRechargeEnabled == null ? !"production".equals(environment) : demoRechargeEnabled;
    }
}
