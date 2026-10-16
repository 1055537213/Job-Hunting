package com.jobhunting.platform.billing;

import java.math.BigDecimal;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.List;
import java.util.Map;
import com.jobhunting.platform.auth.AccountAdministrationService;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Isolation;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.core.type.TypeReference;
import tools.jackson.databind.ObjectMapper;

@Service
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class BillingQueryService {
    public static final int PAGE_SIZE = 100;
    private final JdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final BillingReadSettings settings;

    public BillingQueryService(JdbcTemplate jdbc, ObjectMapper mapper, BillingReadSettings settings) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.settings = settings;
    }

    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public Map<String, Object> summary(AccountAdministrationService.AccountView account) {
        return summary(account.account_id(), account.status());
    }

    private Map<String, Object> summary(long accountId, String accountStatus) {
        var rows = jdbc.query("""
                SELECT b.account_id, b.balance_micro_yuan, b.total_recharge_micro_yuan, b.total_consumed_micro_yuan,
                    (SELECT COUNT(*) FROM account_balance_ledger WHERE account_id = b.account_id) AS ledger_entry_count
                FROM account_balances b WHERE b.account_id = ?
                """, (row, index) -> {
            long amount = row.getLong("balance_micro_yuan");
            long threshold = settings.thresholdMicroYuan();
            String state = !"active".equals(accountStatus) || amount <= 0 ? "suspended"
                    : amount <= threshold ? "low_balance" : "balance";
            return Map.<String, Object>of("account_id", row.getLong("account_id"), "balance_micro_yuan", amount,
                    "total_recharge_micro_yuan", row.getLong("total_recharge_micro_yuan"),
                    "total_consumed_micro_yuan", row.getLong("total_consumed_micro_yuan"),
                    "ledger_entry_count", row.getLong("ledger_entry_count"), "low_balance_threshold_micro_yuan", threshold,
                    "state", state, "state_label", switch (state) {
                        case "balance" -> "余额";
                        case "low_balance" -> "低余额";
                        default -> "停用";
                    });
        }, accountId);
        if (rows.isEmpty()) throw new BillingException("ACCOUNT_NOT_FOUND", "账号余额不存在。", HttpStatus.NOT_FOUND);
        return rows.getFirst();
    }

    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public LedgerPage adminLedger(Long accountId, int limit, long offset) {
        validatePage(limit, offset);
        String where = accountId == null ? "" : " WHERE account_id = ?";
        Object[] filter = accountId == null ? new Object[]{} : new Object[]{accountId};
        long total = jdbc.queryForObject("SELECT count(*) FROM account_balance_ledger" + where, Long.class, filter);
        var entries = jdbc.query("SELECT * FROM account_balance_ledger" + where + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (row, index) -> ledger(row), pageArgs(accountId, limit, offset));
        return new LedgerPage(entries, total, limit, offset, PAGE_SIZE, null);
    }

    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public AdminOrdersPage adminOrders(Long accountId, int limit, long offset) {
        validatePage(limit, offset);
        if (accountId != null) accountStatus(accountId);
        String where = accountId == null ? "" : " WHERE account_id = ?";
        Object[] filter = accountId == null ? new Object[]{} : new Object[]{accountId};
        long total = jdbc.queryForObject("SELECT count(*) FROM recharge_orders" + where, Long.class, filter);
        var orders = jdbc.query("SELECT * FROM recharge_orders" + where + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (row, index) -> order(row), pageArgs(accountId, limit, offset));
        return new AdminOrdersPage(orders, total, limit, offset, PAGE_SIZE, null);
    }

    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public OrderEvents orderEvents(long orderId) {
        var orders = jdbc.query("SELECT * FROM recharge_orders WHERE id = ?", (row, index) -> order(row), orderId);
        if (orders.isEmpty()) throw new BillingException("ORDER_NOT_FOUND", "充值订单不存在。", HttpStatus.NOT_FOUND);
        var events = jdbc.query("SELECT * FROM payment_events WHERE recharge_order_id = ? ORDER BY received_at, id",
                (row, index) -> new PaymentEvent(row.getLong("id"), row.getLong("recharge_order_id"), row.getString("payment_provider"),
                    row.getString("provider_event_id"), row.getString("event_type"), row.getString("processing_status"),
                    row.getBoolean("signature_valid"), row.getString("payload_sha256"), row.getString("error_summary"),
                    details(row), timestamp(row, "received_at"), timestamp(row, "processed_at")), orderId);
        return new OrderEvents(orders.getFirst(), events);
    }

    @Transactional(readOnly = true)
    public CreditResult creditResult(long accountId, long ledgerId) {
        var entries = jdbc.query("SELECT * FROM account_balance_ledger WHERE id = ? AND account_id = ? AND entry_kind = 'adjustment'",
                (row, index) -> ledger(row), ledgerId, accountId);
        if (entries.size() != 1) throw new BillingException("CREDIT_INCOMPLETE", "平台补款流水尚未完成。", HttpStatus.SERVICE_UNAVAILABLE);
        return new CreditResult(entries.getFirst(), summary(accountId, accountStatus(accountId)));
    }

    private String accountStatus(long accountId) {
        var rows = jdbc.query("SELECT status FROM accounts WHERE id = ?", (row, index) -> row.getString(1), accountId);
        if (rows.isEmpty()) throw new BillingException("ACCOUNT_NOT_FOUND", "账号不存在。", HttpStatus.NOT_FOUND);
        return rows.getFirst();
    }

    private static Object[] pageArgs(Long accountId, int limit, long offset) {
        return accountId == null ? new Object[]{limit, offset} : new Object[]{accountId, limit, offset};
    }

    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public BalancePage balance(AccountAdministrationService.AccountView account, int limit, long offset) {
        validatePage(limit, offset);
        var summary = summary(account);
        var entries = jdbc.query("""
                SELECT * FROM account_balance_ledger WHERE account_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
                """, (row, index) -> ledger(row),
                account.account_id(), limit, offset);
        return new BalancePage(summary, entries, ((Number) summary.get("ledger_entry_count")).longValue(),
                limit, offset, PAGE_SIZE, null, settings.publicView());
    }

    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public OrdersPage orders(AccountAdministrationService.AccountView account, int limit, long offset) {
        validatePage(limit, offset);
        long count = jdbc.queryForObject("SELECT COUNT(*) FROM recharge_orders WHERE account_id = ?", Long.class, account.account_id());
        var orders = jdbc.query("""
                SELECT * FROM recharge_orders WHERE account_id = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
                """, (row, index) -> order(row),
                account.account_id(), limit, offset);
        return new OrdersPage(orders, count, limit, offset);
    }

    @Transactional(readOnly = true)
    public RechargeResult rechargeResult(AccountAdministrationService.AccountView account, String key) {
        var orders = jdbc.query("SELECT * FROM recharge_orders WHERE account_id = ? AND idempotency_key = ?",
                (row, index) -> order(row), account.account_id(), key);
        if (orders.size() != 1 || !"paid".equals(orders.getFirst().status())) {
            throw new BillingException("RECHARGE_INCOMPLETE", "模拟充值订单尚未完成，请稍后重试。", HttpStatus.SERVICE_UNAVAILABLE);
        }
        var order = orders.getFirst();
        var entries = jdbc.query("SELECT * FROM account_balance_ledger WHERE account_id = ? AND recharge_order_id = ? AND entry_kind = 'recharge'",
                (row, index) -> ledger(row), account.account_id(), order.id());
        if (entries.size() != 1) {
            throw new BillingException("RECHARGE_INCOMPLETE", "模拟充值流水尚未完成，请稍后重试。", HttpStatus.SERVICE_UNAVAILABLE);
        }
        return new RechargeResult(order, entries.getFirst(), summary(account));
    }

    private LedgerEntry ledger(ResultSet row) throws SQLException {
        return new LedgerEntry(row.getLong("id"), row.getLong("account_id"), row.getString("entry_kind"),
                row.getLong("amount_micro_yuan"), row.getLong("balance_before_micro_yuan"), row.getLong("balance_after_micro_yuan"),
                nullableLong(row, "token_count"), row.getBigDecimal("price_per_million_tokens_yuan"),
                row.getString("source_reference"), row.getString("summary"), nullableLong(row, "operator_account_id"),
                nullableLong(row, "recharge_order_id"), details(row), timestamp(row, "created_at"));
    }

    private RechargeOrder order(ResultSet row) throws SQLException {
        return new RechargeOrder(row.getLong("id"), row.getString("order_number"), row.getLong("account_id"),
                nullableLong(row, "created_by_account_id"), row.getLong("amount_micro_yuan"), row.getString("status"),
                row.getString("payment_provider"), row.getString("provider_order_id"), row.getString("idempotency_key"),
                row.getString("description"), row.getString("failure_reason"), details(row), timestamp(row, "created_at"),
                timestamp(row, "updated_at"), timestamp(row, "paid_at"), timestamp(row, "cancelled_at"), timestamp(row, "refunded_at"));
    }

    private static void validatePage(int limit, long offset) {
        if (limit < 1 || limit > PAGE_SIZE || offset < 0) {
            throw new BillingException("INVALID_REQUEST", "分页参数无效。", HttpStatus.UNPROCESSABLE_ENTITY);
        }
    }

    private Map<String, Object> details(ResultSet row) throws SQLException {
        return mapper.readValue(row.getString("details_json"), new TypeReference<Map<String, Object>>() { });
    }

    private static Long nullableLong(ResultSet row, String column) throws SQLException {
        long value = row.getLong(column);
        return row.wasNull() ? null : value;
    }

    private static String timestamp(ResultSet row, String column) throws SQLException {
        OffsetDateTime time = row.getObject(column, OffsetDateTime.class);
        if (time == null) return null;
        time = time.withOffsetSameInstant(ZoneOffset.UTC);
        return time.format(DateTimeFormatter.ofPattern(time.getNano() == 0
                ? "uuuu-MM-dd'T'HH:mm:ssxxx" : "uuuu-MM-dd'T'HH:mm:ss.SSSSSSxxx"));
    }

    public record LedgerEntry(long id, long account_id, String entry_kind, long amount_micro_yuan,
            long balance_before_micro_yuan, long balance_after_micro_yuan, Long token_count,
            BigDecimal price_per_million_tokens_yuan, String source_reference, String summary,
            Long operator_account_id, Long recharge_order_id, Map<String, Object> details, String created_at) { }
    public record RechargeOrder(long id, String order_number, long account_id, Long created_by_account_id,
            long amount_micro_yuan, String status, String payment_provider, String provider_order_id,
            String idempotency_key, String description, String failure_reason, Map<String, Object> details,
            String created_at, String updated_at, String paid_at, String cancelled_at, String refunded_at) { }
    public record BalancePage(Map<String, Object> summary, List<LedgerEntry> entries, long total, int limit,
            long offset, int page_size, Integer max_pages, Map<String, Object> settings) { }
    public record OrdersPage(List<RechargeOrder> orders, long total, int limit, long offset) { }
    public record RechargeResult(RechargeOrder order, LedgerEntry entry, Map<String, Object> summary) { }
    public record LedgerPage(List<LedgerEntry> entries, long total, int limit, long offset, int page_size, Integer max_pages) { }
    public record AdminOrdersPage(List<RechargeOrder> orders, long total, int limit, long offset, int page_size, Integer max_pages) { }
    public record PaymentEvent(long id, long recharge_order_id, String payment_provider, String provider_event_id,
            String event_type, String processing_status, boolean signature_valid, String payload_sha256,
            String error_summary, Map<String, Object> details, String received_at, String processed_at) { }
    public record OrderEvents(RechargeOrder order, List<PaymentEvent> events) { }
    public record CreditResult(LedgerEntry entry, Map<String, Object> summary) { }
}
