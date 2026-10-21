package com.jobhunting.platform.billing;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.HexFormat;
import java.util.Map;
import java.util.Set;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.dao.EmptyResultDataAccessException;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * Provider-neutral payment callback boundary.
 *
 * The provider-specific adapter only has to normalize its callback to the
 * small JSON contract accepted here. Settlement remains one database
 * transaction and is idempotent on (provider, provider_event_id).
 */
@Service
@ConditionalOnProperty(prefix = "platform.billing", name = "webhook-enabled", havingValue = "true")
public class PaymentWebhookService {
    private static final Set<String> EVENT_TYPES = Set.of(
            "payment.succeeded", "payment.failed", "payment.cancelled");

    private final JdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final PaymentWebhookSettings settings;

    public PaymentWebhookService(JdbcTemplate jdbc, ObjectMapper mapper, PaymentWebhookSettings settings) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.settings = settings;
    }

    @Transactional
    public WebhookResult process(String provider, String timestampHeader, String signatureHeader, byte[] body) {
        String normalizedProvider = normalize(provider, 32, "支付渠道无效。", "INVALID_REQUEST");
        verifySignature(timestampHeader, signatureHeader, body);
        JsonNode payload = parse(body);
        WebhookPayload event = payload(payload);

        var existing = jdbc.query("""
                SELECT e.provider_event_id, e.processing_status, o.order_number, o.status
                FROM payment_events e
                JOIN recharge_orders o ON o.id = e.recharge_order_id
                WHERE e.payment_provider = ? AND e.provider_event_id = ?
                """, (row, index) -> new WebhookResult(
                        normalizedProvider, row.getString("provider_event_id"), row.getString("order_number"),
                        row.getString("status"), true, null), normalizedProvider, event.eventId()).stream().findFirst();
        if (existing.isPresent()) return existing.get();

        var orders = jdbc.query("""
                SELECT id, order_number, account_id, amount_micro_yuan, status
                FROM recharge_orders
                WHERE payment_provider = ? AND provider_order_id = ?
                FOR UPDATE
                """, (row, index) -> new OrderRow(row.getLong("id"), row.getString("order_number"),
                row.getLong("account_id"), row.getLong("amount_micro_yuan"), row.getString("status")),
                normalizedProvider, event.providerOrderId());
        if (orders.isEmpty()) {
            throw new BillingException("PAYMENT_ORDER_NOT_FOUND", "支付订单不存在。", HttpStatus.NOT_FOUND);
        }
        OrderRow order = orders.getFirst();
        if (event.amountMicroYuan() != null && event.amountMicroYuan() != order.amountMicroYuan()) {
            throw new BillingException("PAYMENT_AMOUNT_MISMATCH", "支付金额与订单金额不一致。", HttpStatus.CONFLICT);
        }

        long eventId = insertReceivedEvent(normalizedProvider, event, body, order.id());
        String nextStatus = order.status();
        Long balance = null;
        try {
            switch (event.eventType()) {
                case "payment.succeeded" -> {
                    if ("pending".equals(order.status())) {
                        balance = settlePaid(order);
                        nextStatus = "paid";
                        jdbc.update("""
                                UPDATE recharge_orders
                                SET status='paid', paid_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                                WHERE id=?
                                """, order.id());
                    }
                }
                case "payment.failed" -> {
                    if ("pending".equals(order.status())) {
                        nextStatus = "failed";
                        jdbc.update("""
                                UPDATE recharge_orders
                                SET status='failed', failure_reason=?, updated_at=CURRENT_TIMESTAMP
                                WHERE id=?
                                """, event.failureReason(), order.id());
                    }
                }
                case "payment.cancelled" -> {
                    if ("pending".equals(order.status())) {
                        nextStatus = "cancelled";
                        jdbc.update("""
                                UPDATE recharge_orders
                                SET status='cancelled', cancelled_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                                WHERE id=?
                                """, order.id());
                    }
                }
                default -> throw new IllegalStateException("unsupported payment event");
            }
            jdbc.update("""
                    UPDATE payment_events
                    SET processing_status='processed', processed_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """, eventId);
        } catch (RuntimeException error) {
            jdbc.update("""
                    UPDATE payment_events
                    SET processing_status='failed', error_summary=?, processed_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """, safeError(error), eventId);
            throw error;
        }
        return new WebhookResult(normalizedProvider, event.eventId(), order.orderNumber(), nextStatus, false, balance);
    }

    static String signingPayload(String timestamp, byte[] body) {
        return timestamp + "." + new String(body, StandardCharsets.UTF_8);
    }

    private void verifySignature(String timestampHeader, String signatureHeader, byte[] body) {
        if (settings.secret().isBlank() || timestampHeader == null || signatureHeader == null || body == null) {
            throw signatureError();
        }
        long timestamp;
        try {
            timestamp = Long.parseLong(timestampHeader);
        } catch (NumberFormatException error) {
            throw signatureError();
        }
        if (Math.abs(Instant.now().getEpochSecond() - timestamp) > settings.webhookMaxSkewSeconds()) {
            throw signatureError();
        }
        String supplied = signatureHeader.startsWith("sha256=") ? signatureHeader.substring(7) : "";
        if (!supplied.matches("[0-9a-fA-F]{64}")) throw signatureError();
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(settings.secret().getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            byte[] actual = mac.doFinal(signingPayload(timestampHeader, body).getBytes(StandardCharsets.UTF_8));
            if (!MessageDigest.isEqual(actual, HexFormat.of().parseHex(supplied))) throw signatureError();
        } catch (GeneralSecurityException | IllegalArgumentException error) {
            throw signatureError();
        }
    }

    private JsonNode parse(byte[] body) {
        if (body == null || body.length == 0 || body.length > 64 * 1024) {
            throw new BillingException("INVALID_REQUEST", "支付回调内容无效。", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        try {
            JsonNode node = mapper.readTree(body);
            if (node == null || !node.isObject()) throw new IllegalArgumentException();
            return node;
        } catch (JacksonException | IllegalArgumentException error) {
            throw new BillingException("INVALID_REQUEST", "支付回调内容无效。", HttpStatus.UNPROCESSABLE_ENTITY);
        }
    }

    private WebhookPayload payload(JsonNode node) {
        String eventId = text(node, "event_id", 160);
        String eventType = text(node, "event_type", 64);
        String providerOrderId = text(node, "provider_order_id", 160);
        Long amount = node.get("amount_micro_yuan") == null || node.get("amount_micro_yuan").isNull()
                ? null : number(node.get("amount_micro_yuan"), 1);
        String failureReason = optionalText(node, "failure_reason", 512);
        if (eventId == null || eventType == null || providerOrderId == null || !EVENT_TYPES.contains(eventType)
                || ("payment.succeeded".equals(eventType) && amount == null)) {
            throw new BillingException("INVALID_REQUEST", "支付回调字段无效。", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        return new WebhookPayload(eventId, eventType, providerOrderId, amount, failureReason);
    }

    private long insertReceivedEvent(String provider, WebhookPayload event, byte[] body, long orderId) {
        String details = json(Map.of(
                "event_type", event.eventType(), "provider_order_id", event.providerOrderId(),
                "amount_micro_yuan", event.amountMicroYuan() == null ? 0 : event.amountMicroYuan(),
                "failure_reason", event.failureReason() == null ? "" : event.failureReason()));
        String hash = sha256Hex(body);
        var inserted = jdbc.query("""
                INSERT INTO payment_events (
                    recharge_order_id, payment_provider, provider_event_id, event_type,
                    processing_status, signature_valid, payload_sha256, error_summary,
                    details_json, received_at, processed_at
                ) VALUES (?, ?, ?, ?, 'received', TRUE, ?, NULL, CAST(? AS jsonb), CURRENT_TIMESTAMP, NULL)
                ON CONFLICT (payment_provider, provider_event_id) DO NOTHING
                RETURNING id
                """, (row, index) -> row.getLong("id"), orderId, provider, event.eventId(), event.eventType(), hash, details);
        if (!inserted.isEmpty()) return inserted.getFirst();
        return jdbc.queryForObject("SELECT id FROM payment_events WHERE payment_provider=? AND provider_event_id=?",
                Long.class, provider, event.eventId());
    }

    private Long settlePaid(OrderRow order) {
        BalanceRow balance = lockBalance(order.accountId());
        long after;
        try {
            after = Math.addExact(balance.balanceMicroYuan(), order.amountMicroYuan());
        } catch (ArithmeticException error) {
            throw new BillingException("PAYMENT_STATE_CONFLICT", "支付结算超出余额范围。", HttpStatus.CONFLICT);
        }
        jdbc.update("""
                INSERT INTO account_balance_ledger (
                    account_id, entry_kind, amount_micro_yuan, balance_before_micro_yuan,
                    balance_after_micro_yuan, token_count, price_per_million_tokens_yuan,
                    source_reference, summary, details_json, created_at, recharge_order_id
                ) VALUES (?, 'recharge', ?, ?, ?, NULL, NULL, ?, ?, CAST(? AS jsonb), CURRENT_TIMESTAMP, ?)
                """, order.accountId(), order.amountMicroYuan(), balance.balanceMicroYuan(), after,
                "recharge-order:" + order.orderNumber(), "支付回调充值",
                json(Map.of("payment_provider", "webhook", "order_number", order.orderNumber())), order.id());
        jdbc.update("""
                UPDATE account_balances
                SET balance_micro_yuan=?, total_recharge_micro_yuan=total_recharge_micro_yuan+?, updated_at=CURRENT_TIMESTAMP
                WHERE account_id=?
                """, after, order.amountMicroYuan(), order.accountId());
        return after;
    }

    private BalanceRow lockBalance(long accountId) {
        jdbc.queryForObject("SELECT id FROM accounts WHERE id=? FOR NO KEY UPDATE", Object.class, accountId);
        try {
            return jdbc.queryForObject("SELECT account_id,balance_micro_yuan FROM account_balances WHERE account_id=? FOR UPDATE",
                    (row, index) -> new BalanceRow(row.getLong("account_id"), row.getLong("balance_micro_yuan")), accountId);
        } catch (EmptyResultDataAccessException error) {
            throw new BillingException("ACCOUNT_NOT_FOUND", "账号余额不存在。", HttpStatus.NOT_FOUND);
        }
    }

    private String json(Map<String, Object> value) {
        try { return mapper.writeValueAsString(value); }
        catch (JacksonException error) { throw new IllegalStateException("支付回调明细序列化失败。", error); }
    }

    private String sha256Hex(byte[] value) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value)); }
        catch (GeneralSecurityException error) { throw new IllegalStateException("JVM 不支持 SHA-256。", error); }
    }

    private static String normalize(String value, int max, String message, String code) {
        String result = value == null ? "" : value.strip().toLowerCase();
        if (!result.matches("[a-z0-9_-]{1," + max + "}")) throw new BillingException(code, message, HttpStatus.UNPROCESSABLE_ENTITY);
        return result;
    }

    private static String text(JsonNode node, String field, int max) {
        String value = optionalText(node, field, max);
        return value == null || value.isBlank() ? null : value;
    }

    private static String optionalText(JsonNode node, String field, int max) {
        JsonNode value = node.get(field);
        if (value == null || value.isNull()) return null;
        if (!value.isString() || value.asText().length() > max) return null;
        return value.asText().strip();
    }

    private static Long number(JsonNode node, long min) {
        return node.isIntegralNumber() && node.canConvertToLong() && node.asLong() >= min ? node.asLong() : null;
    }

    private static String safeError(RuntimeException error) {
        return error instanceof BillingException billing ? billing.code() : "PAYMENT_PROCESSING_FAILED";
    }

    private static BillingException signatureError() {
        return new BillingException("PAYMENT_SIGNATURE_INVALID", "支付回调签名无效。", HttpStatus.UNAUTHORIZED);
    }

    public record WebhookResult(String provider, String event_id, String order_number, String status,
            boolean replayed, Long balance_micro_yuan) { }

    private record WebhookPayload(String eventId, String eventType, String providerOrderId,
            Long amountMicroYuan, String failureReason) { }

    private record OrderRow(long id, String orderNumber, long accountId, long amountMicroYuan, String status) { }

    private record BalanceRow(long accountId, long balanceMicroYuan) { }
}
