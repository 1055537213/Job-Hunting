package com.jobhunting.platform.billing;

import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

public final class BillingDtos {

    private BillingDtos() {
    }

    public record ConsumeRequest(
            @NotNull @Min(1) Long account_id,
            @NotNull @Min(1) Long amount_micro_yuan,
            @Min(0) Long token_count,
            @NotBlank @Size(max = 160) String source_reference,
            @Size(max = 512) String description) {
    }

    public record RechargeRequest(
            @NotNull @Min(1) Long account_id,
            @NotNull @Min(1) Long amount_micro_yuan,
            @NotBlank @Size(min = 16, max = 128) String source_reference,
            @Min(1) Long actor_account_id,
            @Min(1) Long max_amount_micro_yuan,
            @Min(1) Long max_total_micro_yuan,
            @Size(max = 512) String description) {
    }

    public record BalanceProjection(
            long account_id,
            long balance_micro_yuan,
            long total_recharge_micro_yuan,
            long total_consumed_micro_yuan,
            long ledger_entry_count) {
    }

    public record ChargeResponse(
            long account_id,
            long balance_micro_yuan,
            long ledger_entry_id,
            boolean replayed,
            long charged_micro_yuan) {
    }

    public record ErrorResponse(String code, String message, String trace_id) {
    }
}
