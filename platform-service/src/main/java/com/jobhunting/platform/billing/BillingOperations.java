package com.jobhunting.platform.billing;

public interface BillingOperations {

    BillingDtos.BalanceProjection getBalance(long accountId);

    BillingDtos.ChargeResponse consume(
            BillingDtos.ConsumeRequest request,
            String idempotencyKey);

    BillingDtos.BalanceProjection recharge(
            BillingDtos.RechargeRequest request,
            String idempotencyKey);
}
