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
        var rows = jdbc.query("""
                SELECT b.account_id, b.balance_micro_yuan, b.total_recharge_micro_yuan, b.total_consumed_micro_yuan,
                    (SELECT COUNT(*) FROM account_balance_ledger WHERE account_id = b.account_id) AS ledger_entry_count
                FROM account_balances b WHERE b.account_id = ?
                """, (row, index) -> {
            long amount = row.getLong("balance_micro_yuan");
            long threshold = settings.thresholdMicroYuan();
            String state = !"active".equals(account.status()) || amount <= 0 ? "suspended"
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
        }, account.account_id());
        if (rows.isEmpty()) throw new BillingException("ACCOUNT_NOT_FOUND", "账号余额不存在。", HttpStatus.NOT_FOUND);
        return rows.getFirst();
    }

    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public BalancePage balance(AccountAdministrationService.AccountView account, int limit, long offset) {
        validatePage(limit, offset);
        var summary = summary(account);
        var entries = jdbc.query("""
                SELECT * FROM account_balance_ledger WHERE account_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
                """, (row, index) -> new LedgerEntry(row.getLong("id"), row.getLong("account_id"), row.getString("entry_kind"),
                row.getLong("amount_micro_yuan"), row.getLong("balance_before_micro_yuan"), row.getLong("balance_after_micro_yuan"),
                nullableLong(row, "token_count"), row.getBigDecimal("price_per_million_tokens_yuan"),
                row.getString("source_reference"), row.getString("summary"), nullableLong(row, "operator_account_id"),
                nullableLong(row, "recharge_order_id"), details(row), timestamp(row, "created_at")),
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
                """, (row, index) -> new RechargeOrder(row.getLong("id"), row.getString("order_number"), row.getLong("account_id"),
                nullableLong(row, "created_by_account_id"), row.getLong("amount_micro_yuan"), row.getString("status"),
                row.getString("payment_provider"), row.getString("provider_order_id"), row.getString("idempotency_key"),
                row.getString("description"), row.getString("failure_reason"), details(row), timestamp(row, "created_at"),
                timestamp(row, "updated_at"), timestamp(row, "paid_at"), timestamp(row, "cancelled_at"), timestamp(row, "refunded_at")),
                account.account_id(), limit, offset);
        return new OrdersPage(orders, count, limit, offset);
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
}
