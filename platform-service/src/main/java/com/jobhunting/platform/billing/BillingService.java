package com.jobhunting.platform.billing;

import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Map;
import java.util.UUID;

import tools.jackson.core.JacksonException;
import tools.jackson.databind.ObjectMapper;

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

    @Transactional
    public BillingDtos.BalanceProjection recharge(
            BillingDtos.RechargeRequest request,
            String idempotencyKey) {
        if (!idempotencyKey.equals(request.source_reference())) {
            throw new BillingException(
                    "INVALID_REQUEST",
                    "充值请求的 Idempotency-Key 必须与 source_reference 一致。",
                    org.springframework.http.HttpStatus.UNPROCESSABLE_ENTITY);
        }

        long accountId = request.account_id();
        long amount = request.amount_micro_yuan();
        ExistingRecharge existing = findExistingRecharge(accountId, request.source_reference());
        if (existing != null) {
            if (existing.amountMicroYuan() != amount || !"simulated".equals(existing.paymentProvider())) {
                throw idempotencyConflict();
            }
            return getBalance(accountId);
        }

        BalanceRow balance = lockBalance(accountId);
        // A concurrent request with the same key may have committed while this request waited.
        existing = findExistingRecharge(accountId, request.source_reference());
        if (existing != null) {
            if (existing.amountMicroYuan() != amount || !"simulated".equals(existing.paymentProvider())) {
                throw idempotencyConflict();
            }
            return getBalance(accountId);
        }

        if (request.max_amount_micro_yuan() != null
                && amount > request.max_amount_micro_yuan()) {
            throw new BillingException(
                    "INVALID_REQUEST",
                    "单笔模拟充值金额超过演示额度上限。",
                    org.springframework.http.HttpStatus.UNPROCESSABLE_ENTITY);
        }
        if (request.max_total_micro_yuan() != null) {
            Long simulatedTotal = jdbcTemplate.queryForObject(
                    """
                    SELECT COALESCE(SUM(amount_micro_yuan), 0)
                    FROM recharge_orders
                    WHERE account_id = ? AND payment_provider = 'simulated' AND status = 'paid'
                    """,
                    Long.class,
                    accountId);
            if (simulatedTotal != null
                    && simulatedTotal + amount > request.max_total_micro_yuan()) {
                throw new BillingException(
                        "INVALID_REQUEST",
                        "该账号已达到模拟充值累计演示额度上限。",
                        org.springframework.http.HttpStatus.UNPROCESSABLE_ENTITY);
            }
        }

        long actorAccountId = request.actor_account_id() == null
                ? accountId
                : request.actor_account_id();
        String orderNumber = "recharge-" + UUID.randomUUID().toString().replace("-", "");
        String description = request.description() == null || request.description().isBlank()
                ? "个人中心模拟充值"
                : request.description().trim();
        long after = balance.balanceMicroYuan() + amount;
        long orderId = jdbcTemplate.queryForObject(
                """
                INSERT INTO recharge_orders (
                    order_number, account_id, created_by_account_id, amount_micro_yuan,
                    status, payment_provider, provider_order_id, idempotency_key,
                    description, failure_reason, details_json, created_at, updated_at,
                    paid_at, cancelled_at, refunded_at
                ) VALUES (?, ?, ?, ?, 'paid', 'simulated', ?, ?, ?, NULL,
                          CAST(? AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                          CURRENT_TIMESTAMP, NULL, NULL)
                RETURNING id
                """,
                Long.class,
                orderNumber,
                accountId,
                actorAccountId,
                amount,
                orderNumber,
                request.source_reference(),
                description,
                serializeRechargeDetails(orderNumber, request.source_reference(), amount));

        long ledgerId = jdbcTemplate.queryForObject(
                """
                INSERT INTO account_balance_ledger (
                    account_id, entry_kind, amount_micro_yuan,
                    balance_before_micro_yuan, balance_after_micro_yuan,
                    token_count, price_per_million_tokens_yuan,
                    source_reference, summary, details_json, created_at, recharge_order_id
                ) VALUES (?, 'recharge', ?, ?, ?, NULL, NULL, ?, ?, CAST(? AS jsonb),
                          CURRENT_TIMESTAMP, ?)
                RETURNING id
                """,
                Long.class,
                accountId,
                amount,
                balance.balanceMicroYuan(),
                after,
                "recharge-order:" + orderNumber,
                description,
                serializeRechargeDetails(orderNumber, request.source_reference(), amount),
                orderId);

        jdbcTemplate.update(
                """
                UPDATE account_balances
                SET balance_micro_yuan = ?,
                    total_recharge_micro_yuan = total_recharge_micro_yuan + ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE account_id = ?
                """,
                after,
                amount,
                accountId);
        jdbcTemplate.update(
                """
                INSERT INTO payment_events (
                    recharge_order_id, payment_provider, provider_event_id, event_type,
                    processing_status, signature_valid, payload_sha256, error_summary,
                    details_json, received_at, processed_at
                ) VALUES (?, 'simulated', ?, 'payment.succeeded', 'processed', TRUE,
                          ?, NULL, CAST(? AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                orderId,
                "simulated:" + orderNumber,
                sha256Hex(orderNumber + ":" + amount + ":paid"),
                serializeRechargeDetails(orderNumber, request.source_reference(), amount));

        // Keep the response shape identical for a first request and an idempotent replay.
        return getBalance(accountId);
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

    private ExistingRecharge findExistingRecharge(long accountId, String idempotencyKey) {
        return jdbcTemplate.query(
                """
                SELECT amount_micro_yuan, payment_provider
                FROM recharge_orders
                WHERE account_id = ? AND idempotency_key = ?
                """,
                (result, rowNum) -> new ExistingRecharge(
                        result.getLong("amount_micro_yuan"),
                        result.getString("payment_provider")),
                accountId,
                idempotencyKey).stream().findFirst().orElse(null);
    }

    private String serializeDetails(BillingDtos.ConsumeRequest request, long amount) {
        try {
            return objectMapper.writeValueAsString(Map.of(
                    "source_reference", request.source_reference(),
                    "token_count", request.token_count() == null ? 0 : request.token_count(),
                    "consumption_micro_yuan", amount));
        } catch (JacksonException exception) {
            throw new IllegalStateException("账务明细序列化失败。", exception);
        }
    }

    private String serializeRechargeDetails(String orderNumber, String sourceReference, long amount) {
        try {
            return objectMapper.writeValueAsString(Map.of(
                    "payment_provider", "simulated",
                    "order_number", orderNumber,
                    "source_reference", sourceReference,
                    "amount_micro_yuan", amount));
        } catch (JacksonException exception) {
            throw new IllegalStateException("充值明细序列化失败。", exception);
        }
    }

    private String sha256Hex(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(digest.length * 2);
            for (byte item : digest) {
                hex.append(String.format("%02x", item));
            }
            return hex.toString();
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("JVM 不支持 SHA-256。", exception);
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

    private record ExistingRecharge(long amountMicroYuan, String paymentProvider) {
    }
}
