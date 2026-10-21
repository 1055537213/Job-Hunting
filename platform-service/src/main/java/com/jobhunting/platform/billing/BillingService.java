package com.jobhunting.platform.billing;

import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Map;
import java.util.UUID;

import com.jobhunting.platform.auth.SessionService;

import tools.jackson.core.JacksonException;
import tools.jackson.databind.ObjectMapper;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.beans.factory.ObjectProvider;
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
    private final ObjectProvider<SessionService> sessions;

    public BillingService(
            JdbcTemplate jdbcTemplate,
            ObjectMapper objectMapper,
            @Value("${platform.billing.price-per-million-tokens-yuan:25.0}") BigDecimal pricePerMillionTokensYuan,
            ObjectProvider<SessionService> sessions) {
        this.jdbcTemplate = jdbcTemplate;
        this.objectMapper = objectMapper;
        this.pricePerMillionTokensYuan = pricePerMillionTokensYuan;
        this.sessions = sessions;
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
                    && (simulatedTotal > request.max_total_micro_yuan()
                        || amount > request.max_total_micro_yuan() - simulatedTotal)) {
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
        long after;
        try {
            after = Math.addExact(balance.balanceMicroYuan(), amount);
        } catch (ArithmeticException exception) {
            throw new BillingException("INVALID_REQUEST", "充值后的余额超出支持范围。",
                    org.springframework.http.HttpStatus.UNPROCESSABLE_ENTITY);
        }
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

    @Transactional
    public BillingDtos.PendingOrderResponse createPendingOrder(
            BillingDtos.PendingOrderRequest request, String idempotencyKey) {
        if (!idempotencyKey.equals(request.source_reference())) {
            throw new BillingException("INVALID_REQUEST", "订单幂等键必须与 source_reference 一致。",
                    org.springframework.http.HttpStatus.UNPROCESSABLE_ENTITY);
        }
        String provider = request.payment_provider().strip().toLowerCase();
        if (!provider.matches("[a-z0-9_-]{1,32}")) {
            throw new BillingException("INVALID_REQUEST", "支付渠道无效。",
                    org.springframework.http.HttpStatus.UNPROCESSABLE_ENTITY);
        }
        var existing = jdbcTemplate.query("""
                SELECT id, order_number, account_id, amount_micro_yuan, status,
                       payment_provider, provider_order_id, idempotency_key
                FROM recharge_orders WHERE account_id=? AND idempotency_key=?
                """, (row, index) -> new BillingDtos.PendingOrderResponse(
                row.getLong("id"), row.getString("order_number"), row.getLong("account_id"),
                row.getLong("amount_micro_yuan"), row.getString("status"), row.getString("payment_provider"),
                row.getString("provider_order_id"), row.getString("idempotency_key")),
                request.account_id(), request.source_reference()).stream().findFirst();
        if (existing.isPresent()) {
            var order = existing.get();
            if (order.amount_micro_yuan() != request.amount_micro_yuan()
                    || !provider.equals(order.payment_provider())) {
                throw new BillingException("IDEMPOTENCY_CONFLICT", "该幂等键已用于另一笔支付订单。",
                        org.springframework.http.HttpStatus.CONFLICT);
            }
            return order;
        }
        lockBalance(request.account_id());
        String orderNumber = "recharge-" + UUID.randomUUID().toString().replace("-", "");
        String description = request.description() == null || request.description().isBlank()
                ? "待支付充值订单" : request.description().strip();
        try {
            Long id = jdbcTemplate.queryForObject("""
                    INSERT INTO recharge_orders (
                        order_number, account_id, created_by_account_id, amount_micro_yuan,
                        status, payment_provider, provider_order_id, idempotency_key,
                        description, failure_reason, details_json, created_at, updated_at,
                        paid_at, cancelled_at, refunded_at
                    ) VALUES (?, ?, NULL, ?, 'pending', ?, ?, ?, ?, NULL, CAST(? AS jsonb),
                              CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL, NULL, NULL)
                    RETURNING id
                    """, Long.class, orderNumber, request.account_id(), request.amount_micro_yuan(), provider,
                    orderNumber, request.source_reference(), description,
                    json(Map.of("payment_provider", provider, "order_number", orderNumber,
                            "settlement", "signed_webhook")));
            return new BillingDtos.PendingOrderResponse(id, orderNumber, request.account_id(),
                    request.amount_micro_yuan(), "pending", provider, orderNumber, request.source_reference());
        } catch (DuplicateKeyException exception) {
            throw new BillingException("IDEMPOTENCY_CONFLICT", "该幂等键已用于另一笔支付订单。",
                    org.springframework.http.HttpStatus.CONFLICT);
        }
    }

    @Transactional
    public BillingDtos.RefundResponse refund(
            BillingDtos.RefundRequest request, String idempotencyKey) {
        if (!idempotencyKey.equals(request.source_reference())) {
            throw new BillingException("INVALID_REQUEST", "退款请求的 Idempotency-Key 必须与 source_reference 一致。",
                    org.springframework.http.HttpStatus.UNPROCESSABLE_ENTITY);
        }

        long accountId = request.account_id();
        long orderId = request.recharge_order_id();
        long amount = request.amount_micro_yuan();
        ExistingLedger existing = findExistingLedger(request.source_reference());
        if (existing != null) {
            if (existing.accountId() != accountId || existing.entryKind() == null
                    || !"adjustment".equals(existing.entryKind())
                    || existing.amountMicroYuan() != -amount) {
                throw refundConflict();
            }
            return new BillingDtos.RefundResponse(accountId, existing.balanceAfterMicroYuan(), existing.id(), true,
                    amount, orderId);
        }

        RefundOrder order = jdbcTemplate.query(
                """
                SELECT id, account_id, amount_micro_yuan, status, payment_provider
                FROM recharge_orders
                WHERE id = ? AND account_id = ?
                FOR UPDATE
                """,
                (result, rowNum) -> new RefundOrder(
                        result.getLong("id"), result.getLong("account_id"),
                        result.getLong("amount_micro_yuan"), result.getString("status"),
                        result.getString("payment_provider")),
                orderId, accountId).stream().findFirst().orElse(null);
        if (order == null) {
            throw new BillingException("ORDER_NOT_FOUND", "充值订单不存在。", org.springframework.http.HttpStatus.NOT_FOUND);
        }
        if (!"paid".equals(order.status())) {
            if ("refunded".equals(order.status())) {
                throw new BillingException("REFUND_ALREADY_APPLIED", "该充值订单已经退款。",
                        org.springframework.http.HttpStatus.CONFLICT);
            }
            throw new BillingException("REFUND_ORDER_NOT_PAID", "只有已到账的充值订单可以退款。",
                    org.springframework.http.HttpStatus.CONFLICT);
        }
        if (order.amountMicroYuan() != amount) {
            throw new BillingException("REFUND_AMOUNT_MISMATCH", "退款金额必须与充值订单金额一致。",
                    org.springframework.http.HttpStatus.CONFLICT);
        }

        // Recheck after the order lock so a concurrent retry returns the original result.
        existing = findExistingLedger(request.source_reference());
        if (existing != null) {
            if (existing.accountId() != accountId || !"adjustment".equals(existing.entryKind())
                    || existing.amountMicroYuan() != -amount) {
                throw refundConflict();
            }
            return new BillingDtos.RefundResponse(accountId, existing.balanceAfterMicroYuan(), existing.id(), true,
                    amount, orderId);
        }

        BalanceRow balance = lockBalance(accountId);
        if (balance.balanceMicroYuan() < amount) {
            throw new BillingException("REFUND_BALANCE_INSUFFICIENT", "当前余额低于待退款金额，暂时无法退款。",
                    org.springframework.http.HttpStatus.CONFLICT);
        }
        long after = balance.balanceMicroYuan() - amount;
        String description = request.description() == null || request.description().isBlank()
                ? "充值订单退款" : request.description().trim();
        String details = serializeRefundDetails(order, request.source_reference(), amount, description);
        long ledgerId = jdbcTemplate.queryForObject(
                """
                INSERT INTO account_balance_ledger (
                    account_id, entry_kind, amount_micro_yuan,
                    balance_before_micro_yuan, balance_after_micro_yuan,
                    token_count, price_per_million_tokens_yuan,
                    source_reference, summary, details_json, created_at, recharge_order_id
                ) VALUES (?, 'adjustment', ?, ?, ?, NULL, NULL, ?, ?, CAST(? AS jsonb),
                          CURRENT_TIMESTAMP, ?)
                RETURNING id
                """,
                Long.class, accountId, -amount, balance.balanceMicroYuan(), after,
                request.source_reference(), description, details, orderId);
        jdbcTemplate.update(
                """
                UPDATE account_balances
                SET balance_micro_yuan = ?, updated_at = CURRENT_TIMESTAMP
                WHERE account_id = ?
                """,
                after, accountId);
        jdbcTemplate.update(
                """
                UPDATE recharge_orders
                SET status = 'refunded', refunded_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                orderId);
        jdbcTemplate.update(
                """
                INSERT INTO payment_events (
                    recharge_order_id, payment_provider, provider_event_id, event_type,
                    processing_status, signature_valid, payload_sha256, error_summary,
                    details_json, received_at, processed_at
                ) VALUES (?, ?, ?, 'payment.refunded', 'processed', TRUE, ?, NULL,
                          CAST(? AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                orderId, order.paymentProvider(), "internal-refund:" + request.source_reference(),
                sha256Hex(request.source_reference() + ":" + amount + ":refunded"), details);
        return new BillingDtos.RefundResponse(accountId, after, ledgerId, false, amount, orderId);
    }

    @Transactional
    public BillingDtos.CreditResponse credit(
            BillingDtos.CreditRequest request, String idempotencyKey, String traceId) {
        String key = request.idempotency_key().strip();
        String reason = request.reason().strip();
        if (!key.equals(idempotencyKey) || key.length() < 16 || key.length() > 128
                || !key.codePoints().allMatch(c -> Character.isLetterOrDigit(c) || c == '-' || c == '_' || c == ':')
                || reason.length() < 2 || reason.length() > 500
                || request.amount_micro_yuan() <= 0 || request.amount_micro_yuan() > 1_000_000_000_000L) {
            throw new BillingException("INVALID_REQUEST", "补款金额、原因或幂等键无效。",
                    org.springframework.http.HttpStatus.UNPROCESSABLE_ENTITY);
        }
        SessionService auth = sessions.getIfAvailable();
        if (auth == null) {
            throw new BillingException("AUTH_UNAVAILABLE", "补款需要启用 Java 认证服务。",
                    org.springframework.http.HttpStatus.SERVICE_UNAVAILABLE);
        }
        // Match account administration's lock order, including cross-admin credits and disables.
        jdbcTemplate.queryForObject("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
                Object.class, "platform-admin-policy-v1");
        long actorId = auth.requireSession(request.session_token());
        String actorRole = jdbcTemplate.queryForObject("SELECT role FROM accounts WHERE id = ?", String.class, actorId);
        if (!"admin".equals(actorRole) || actorId != request.actor_account_id()) {
            throw new BillingException("ADMIN_REQUIRED", "需要管理员权限。",
                    org.springframework.http.HttpStatus.FORBIDDEN);
        }
        long accountId = request.account_id();
        var targetRoles = jdbcTemplate.query("""
                SELECT role FROM accounts WHERE id = ? AND deleted_at IS NULL FOR NO KEY UPDATE
                """, (row, index) -> row.getString("role"), accountId);
        if (targetRoles.isEmpty()) {
            throw new BillingException("ACCOUNT_NOT_FOUND", "账号不存在。", org.springframework.http.HttpStatus.NOT_FOUND);
        }
        String reference = "admin-credit:" + actorId + ":" + key;
        ExistingCredit existing = findExistingCredit(reference);
        if (existing != null) return replayCredit(existing, request, reason);
        BalanceRow balance = lockBalance(accountId);
        existing = findExistingCredit(reference);
        if (existing != null) return replayCredit(existing, request, reason);
        long after;
        try {
            after = Math.addExact(balance.balanceMicroYuan(), request.amount_micro_yuan());
        } catch (ArithmeticException exception) {
            throw new BillingException("INVALID_REQUEST", "补款后的余额超出支持范围。",
                    org.springframework.http.HttpStatus.UNPROCESSABLE_ENTITY);
        }
        long ledgerId;
        try {
            ledgerId = jdbcTemplate.queryForObject("""
                    INSERT INTO account_balance_ledger (
                        account_id, entry_kind, amount_micro_yuan, balance_before_micro_yuan,
                        balance_after_micro_yuan, source_reference, summary, details_json,
                        operator_account_id, created_at
                    ) VALUES (?, 'adjustment', ?, ?, ?, ?, ?, CAST(? AS jsonb), ?, CURRENT_TIMESTAMP)
                    RETURNING id
                    """, Long.class, accountId, request.amount_micro_yuan(), balance.balanceMicroYuan(), after,
                    reference, "管理员人工补款", json(Map.of("reason", reason, "adjustment_type", "manual_credit")), actorId);
        } catch (DuplicateKeyException exception) {
            throw creditConflict();
        }
        jdbcTemplate.update("UPDATE account_balances SET balance_micro_yuan = ?, updated_at = CURRENT_TIMESTAMP WHERE account_id = ?",
                after, accountId);
        jdbcTemplate.update("""
                INSERT INTO admin_audit_events (
                    actor_account_id, target_account_id, action, target_type, target_id,
                    outcome, summary, details_json, request_id, created_at
                ) VALUES (?, ?, 'balance.manual_credit', 'account_balance', ?, 'succeeded', ?,
                          CAST(? AS jsonb), ?, CURRENT_TIMESTAMP)
                """, actorId, accountId, Long.toString(accountId), "管理员为账号 #" + accountId + " 执行人工补款。",
                json(Map.of("target_role", targetRoles.getFirst(), "amount_micro_yuan", request.amount_micro_yuan(),
                        "balance_before_micro_yuan", balance.balanceMicroYuan(), "balance_after_micro_yuan", after,
                        "reason", reason, "ledger_reference", reference)),
                traceId != null && traceId.matches("[A-Za-z0-9._:-]{1,128}") ? traceId : null);
        return new BillingDtos.CreditResponse(accountId, after, ledgerId, false);
    }

    private ExistingCredit findExistingCredit(String reference) {
        return jdbcTemplate.query("""
                SELECT id, account_id, entry_kind, amount_micro_yuan, balance_after_micro_yuan,
                       operator_account_id, details_json->>'reason' AS reason,
                       details_json->>'adjustment_type' AS adjustment_type
                FROM account_balance_ledger WHERE source_reference = ?
                """, (row, index) -> new ExistingCredit(row.getLong("id"), row.getLong("account_id"),
                row.getString("entry_kind"), row.getLong("amount_micro_yuan"), row.getLong("balance_after_micro_yuan"),
                row.getLong("operator_account_id"), row.getString("reason"), row.getString("adjustment_type")),
                reference).stream().findFirst().orElse(null);
    }

    private BillingDtos.CreditResponse replayCredit(ExistingCredit existing, BillingDtos.CreditRequest request, String reason) {
        if (existing.accountId() != request.account_id() || existing.actorId() != request.actor_account_id()
                || existing.amount() != request.amount_micro_yuan() || !"adjustment".equals(existing.kind())
                || !"manual_credit".equals(existing.adjustmentType()) || !reason.equals(existing.reason())) {
            throw creditConflict();
        }
        return new BillingDtos.CreditResponse(existing.accountId(), existing.balanceAfter(), existing.id(), true);
    }

    private BillingException creditConflict() {
        return new BillingException("IDEMPOTENCY_CONFLICT", "该幂等键已用于另一笔管理员补款。",
                org.springframework.http.HttpStatus.CONFLICT);
    }

    private String json(Map<String, Object> details) {
        try {
            return objectMapper.writeValueAsString(details);
        } catch (JacksonException exception) {
            throw new IllegalStateException("补款明细序列化失败。", exception);
        }
    }

    private record ExistingCredit(long id, long accountId, String kind, long amount, long balanceAfter,
            long actorId, String reason, String adjustmentType) { }

    private BalanceRow lockBalance(long accountId) {
        // Account before balance; NO KEY UPDATE remains compatible with ledger foreign-key checks.
        jdbcTemplate.query("SELECT id FROM accounts WHERE id = ? FOR NO KEY UPDATE",
                (row, index) -> row.getLong(1), accountId);
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

    private String serializeRefundDetails(RefundOrder order, String sourceReference, long amount, String description) {
        try {
            return objectMapper.writeValueAsString(Map.of(
                    "payment_provider", order.paymentProvider(),
                    "recharge_order_id", order.id(),
                    "source_reference", sourceReference,
                    "amount_micro_yuan", amount,
                    "description", description));
        } catch (JacksonException exception) {
            throw new IllegalStateException("退款明细序列化失败。", exception);
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

    private BillingException refundConflict() {
        return new BillingException(
                "IDEMPOTENCY_CONFLICT",
                "该退款幂等键已用于另一笔退款。",
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

    private record RefundOrder(long id, long accountId, long amountMicroYuan, String status,
            String paymentProvider) {
    }
}
