package com.jobhunting.platform.billing;

import jakarta.validation.constraints.Pattern;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/payments/webhooks")
@ConditionalOnProperty(prefix = "platform.billing", name = "webhook-enabled", havingValue = "true")
public class PaymentWebhookController {
    private final PaymentWebhookService service;

    public PaymentWebhookController(PaymentWebhookService service) {
        this.service = service;
    }

    @PostMapping(value = "/{provider}", consumes = MediaType.APPLICATION_JSON_VALUE)
    public PaymentWebhookService.WebhookResult receive(
            @PathVariable @Pattern(regexp = "[a-z0-9_-]{1,32}") String provider,
            @RequestHeader("X-Payment-Timestamp") String timestamp,
            @RequestHeader("X-Payment-Signature") String signature,
            @RequestBody byte[] body) {
        return service.process(provider, timestamp, signature, body);
    }
}
