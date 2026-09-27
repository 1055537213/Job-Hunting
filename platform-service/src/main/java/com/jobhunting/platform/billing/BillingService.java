package com.jobhunting.platform.billing;

import java.math.BigDecimal;
import java.util.Map;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.dao.EmptyResultDataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
@ConditionalOnProperty(prefix = "platform.billing", name = "enabled", havingValue = "true")
public class BillingService implements BillingOperations {

    private final JdbcTemplate jdbcTemplate;
    private final ObjectMapper objectMapper;
    private final BigDecimal pricePerMillionTokensYuan;

    public BillingService(
            JdbcTemplate jdbcTemplate,
            ObjectMapper objectMapper,
            @Value("${platform.billing.price-per-million-tokens-yuan:25.0}") BigDecimal pricePerMillionTokensYuan) {
        this.jdbcTemplate = jdbcTemplate;
        this.objectMapper = objectMapper;
        this.pricePerMillionTokensYuan = pricePerMillionTokensYuan;
    }

    @Transactional(readOnly = true)
    public BillingDtos.BalanceProjection getBalance(long accountId) {
        try {
            return jdbcTemplate.queryForObject(
                    """
                    SELECT b.account_id, b.balance_micro_yuan,
                           b.total_recharge_micro_yuan, b.total_consumed_micro_yuan,
                           COUNT(l.id) AS ledger_entry_count
                    FROM account_balances b
                    LEFT JOIN account_balance_ledger l ON l.account_id = b.account_id
                    WHERE b.account_id = ?
                    GROUP BY b.account_id, b.balance_micro_yuan,
                             b.total_recharge_micro_yuan, b.total_consumed_micro_yuan
                    """,
                    (result, rowNum) -> new BillingDtos.BalanceProjection(
                            result.getLong("account_id"),
                            result.getLong("balance_micro_yuan"),
                            result.getLong("total_recharge_micro_yuan"),
                            result.getLong("total_consumed_micro_yuan"),
                            result.getLong("ledger_entry_count")),
                    accountId);
        } catch (EmptyResultDataAccessException exception) {
            throw new BillingException(
                    "ACCOUNT_NOT_FOUND",
                    "账号余额不存在。",
                    org.springframework.http.HttpStatus.NOT_FOUND);
        }
    }

    @Transactional
    public BillingDtos.ChargeResponse consume(
            BillingDtos.ConsumeRequest request,
            String idempotencyKey) {
        if (!idempotencyKey.equals(request.source_reference())) {
            throw new BillingException(
                    "INVALID_REQUEST",
                    "消费请求的 Idempotency-Key 必须与 source_reference 一致。",
                    org.springframework.http.HttpStatus.UNPROCESSABLE_ENTITY);
        }

        long accountId = request.account_id();
        long amount = request.amount_micro_yuan();
        long tokenCount = request.token_count() == null ? 0 : request.token_count();
        String sourceReference = request.source_reference();
        ExistingLedger existing = findExistingLedger(sourceReference);
        if (existing != null) {
            if (existing.accountId() != accountId
                    || !"consumption".equals(existing.entryKind())
                    || existing.amountMicroYuan() != -amount) {
                throw idempotencyConflict();
            }
            return new BillingDtos.ChargeResponse(
                    accountId,
                    existing.balanceAfterMicroYuan(),
                    existing.id(),
                    true,
                    amount);
        }

        BalanceRow balance = lockBalance(accountId);
        // A concurrent request with the same key may have committed while this one waited for the balance lock.
        existing = findExistingLedger(sourceReference);
        if (existing != null) {
            if (existing.accountId() != accountId
                    || !"consumption".equals(existing.entryKind())
                    || existing.amountMicroYuan() != -amount) {
                throw idempotencyConflict();
            }
            return new BillingDtos.ChargeResponse(
                    accountId,
                    existing.balanceAfterMicroYuan(),
                    existing.id(),
                    true,
                    amount);
        }
        if (balance.balanceMicroYuan() < amount) {
            throw new BillingException(
                    "INSUFFICIENT_BALANCE",
                    "余额不足，请先充值后重试",
                    org.springframework.http.HttpStatus.CONFLICT);
        }

        long after = balance.balanceMicroYuan() - amount;
        jdbcTemplate.update(
                """
                UPDATE account_balances
                SET balance_micro_yuan = ?,
                    total_consumed_micro_yuan = total_consumed_micro_yuan + ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE account_id = ?
                """,
                after,
                amount,
                accountId);

        String details = serializeDetails(request, amount);
        long ledgerId;
        try {
            ledgerId = jdbcTemplate.queryForObject(
                    """
                INSERT INTO account_balance_ledger (
                    account_id, entry_kind, amount_micro_yuan,
                    balance_before_micro_yuan, balance_after_micro_yuan,
                    token_count, price_per_million_tokens_yuan,
                    source_reference, summary, details_json, created_at
                ) VALUES (?, 'consumption', ?, ?, ?, ?, ?, ?, ?, CAST(? AS jsonb), CURRENT_TIMESTAMP)
                RETURNING id
                    """,
                    Long.class,
                    accountId,
                    -amount,
                    balance.balanceMicroYuan(),
                    after,
                    tokenCount,
                    pricePerMillionTokensYuan,
                    sourceReference,
                    request.description() == null || request.description().isBlank()
                            ? "模型调用扣费"
                            : request.description().trim(),
                    details);
        } catch (DuplicateKeyException exception) {
            throw idempotencyConflict();
        }

        return new BillingDtos.ChargeResponse(accountId, after, ledgerId, false, amount);
    }

    private BalanceRow lockBalance(long accountId) {
        try {
            return jdbcTemplate.queryForObject(
                    """
                    SELECT account_id, balance_micro_yuan
                    FROM account_balances
                    WHERE account_id = ?
                    FOR UPDATE
                    """,
                    (result, rowNum) -> new BalanceRow(
                            result.getLong("account_id"),
                            result.getLong("balance_micro_yuan")),
                    accountId);
        } catch (EmptyResultDataAccessException exception) {
            throw new BillingException(
                    "ACCOUNT_NOT_FOUND",
                    "账号余额不存在。",
                    org.springframework.http.HttpStatus.NOT_FOUND);
        }
    }

    private ExistingLedger findExistingLedger(String sourceReference) {
        return jdbcTemplate.query(
                """
                SELECT id, account_id, entry_kind, amount_micro_yuan, balance_after_micro_yuan
                FROM account_balance_ledger
                WHERE source_reference = ?
                """,
                (result, rowNum) -> new ExistingLedger(
                        result.getLong("id"),
                        result.getLong("account_id"),
                        result.getString("entry_kind"),
                        result.getLong("amount_micro_yuan"),
                        result.getLong("balance_after_micro_yuan")),
                sourceReference).stream().findFirst().orElse(null);
    }

    private String serializeDetails(BillingDtos.ConsumeRequest request, long amount) {
        try {
            return objectMapper.writeValueAsString(Map.of(
                    "source_reference", request.source_reference(),
                    "token_count", request.token_count() == null ? 0 : request.token_count(),
                    "consumption_micro_yuan", amount));
        } catch (JsonProcessingException exception) {
            throw new IllegalStateException("账务明细序列化失败。", exception);
        }
    }

    private BillingException idempotencyConflict() {
        return new BillingException(
                "IDEMPOTENCY_CONFLICT",
                "该幂等键已用于另一笔消费请求。",
                org.springframework.http.HttpStatus.CONFLICT);
    }

    private record BalanceRow(long accountId, long balanceMicroYuan) {
    }

    private record ExistingLedger(
            long id,
            long accountId,
            String entryKind,
            long amountMicroYuan,
            long balanceAfterMicroYuan) {
    }
}
