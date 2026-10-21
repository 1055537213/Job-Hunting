package com.jobhunting.platform.billing;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.context.properties.bind.DefaultValue;
import org.springframework.validation.annotation.Validated;

import jakarta.validation.constraints.Min;

@Validated
@ConfigurationProperties("platform.billing")
public record PaymentWebhookSettings(
        @DefaultValue("false") Boolean webhookEnabled,
        String webhookSecret,
        @DefaultValue("300") @Min(30) int webhookMaxSkewSeconds) {

    public boolean enabled() {
        return Boolean.TRUE.equals(webhookEnabled);
    }

    public String secret() {
        return webhookSecret == null ? "" : webhookSecret;
    }
}
