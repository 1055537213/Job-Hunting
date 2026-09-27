package com.jobhunting.platform.billing;

import jakarta.validation.Valid;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/internal/v1/billing")
@ConditionalOnProperty(prefix = "platform.billing", name = "enabled", havingValue = "true")
public class BillingController {

    private final BillingOperations billingService;
    private final InternalTokenVerifier tokenVerifier;

    public BillingController(BillingOperations billingService, InternalTokenVerifier tokenVerifier) {
        this.billingService = billingService;
        this.tokenVerifier = tokenVerifier;
    }

    @GetMapping("/accounts/{accountId}/balance")
    public BillingDtos.BalanceProjection getBalance(
            @PathVariable long accountId,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String internalToken) {
        tokenVerifier.verify(internalToken);
        return billingService.getBalance(accountId);
    }

    @PostMapping("/consume")
    public ResponseEntity<BillingDtos.ChargeResponse> consume(
            @Valid @RequestBody BillingDtos.ConsumeRequest request,
            @RequestHeader("Idempotency-Key") String idempotencyKey,
            @RequestHeader("X-Trace-Id") String traceId,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String internalToken) {
        tokenVerifier.verify(internalToken);
        return ResponseEntity.ok(billingService.consume(request, idempotencyKey));
    }

    @PostMapping("/recharge")
    public ResponseEntity<BillingDtos.BalanceProjection> recharge(
            @Valid @RequestBody BillingDtos.RechargeRequest request,
            @RequestHeader("Idempotency-Key") String idempotencyKey,
            @RequestHeader("X-Trace-Id") String traceId,
            @RequestHeader(name = "X-Internal-Service-Token", required = false) String internalToken) {
        tokenVerifier.verify(internalToken);
        return ResponseEntity.ok(billingService.recharge(request, idempotencyKey));
    }
}
