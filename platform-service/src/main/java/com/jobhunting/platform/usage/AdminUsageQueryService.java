package com.jobhunting.platform.usage;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import com.jobhunting.platform.auth.AuthException;
import com.jobhunting.platform.billing.BillingReadSettings;
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
public class AdminUsageQueryService {
    public static final int PAGE_SIZE = 100;
    public static final int MAX_PAGES = 5;
    // Python owns insertion and physical retention. Reads also bound historical rows before filtering.
    private static final String WINDOW = """
            WITH ranked AS (SELECT *, ROW_NUMBER() OVER
              (PARTITION BY account_id ORDER BY created_at DESC, id DESC) AS position FROM usage_events),
            retained AS (SELECT * FROM ranked WHERE position <= 500)
            """;
    private static final String TOTALS = """
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(total_tokens), 0) AS total_tokens,
            COALESCE(SUM(CASE WHEN billable THEN total_tokens ELSE 0 END), 0) AS billable_tokens,
            COUNT(*) AS event_count
            """;
    private final JdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final BillingReadSettings settings;

    public AdminUsageQueryService(JdbcTemplate jdbc, ObjectMapper mapper, BillingReadSettings settings) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.settings = settings;
    }

    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public Map<String, Object> summary() {
        var totals = jdbc.queryForMap(WINDOW + " SELECT " + TOTALS + " FROM retained");
        var byAccount = jdbc.queryForList(WINDOW + " SELECT account_id, " + TOTALS + " FROM retained GROUP BY account_id ORDER BY account_id");
        var tools = jdbc.queryForList("""
                WITH ranked AS (SELECT account_id, status, ROW_NUMBER() OVER
                  (PARTITION BY account_id ORDER BY updated_at DESC, id DESC) AS position FROM tool_call_traces)
                SELECT account_id, COUNT(*) AS trace_count,
                  SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_trace_count
                FROM ranked WHERE position <= 500 GROUP BY account_id ORDER BY account_id
                """);
        var balances = jdbc.query("""
                SELECT a.id AS account_id, a.email, a.role, a.status,
                  COALESCE(b.balance_micro_yuan, 0) AS balance_micro_yuan,
                  COALESCE(b.total_recharge_micro_yuan, 0) AS total_recharge_micro_yuan,
                  COALESCE(b.total_consumed_micro_yuan, 0) AS total_consumed_micro_yuan,
                  COALESCE(NULLIF(b.low_balance_threshold_micro_yuan, 0), ?) AS low_balance_threshold_micro_yuan,
                  COALESCE(l.entry_count, 0) AS ledger_entry_count
                FROM accounts a LEFT JOIN account_balances b ON b.account_id = a.id
                LEFT JOIN (SELECT account_id, COUNT(*) AS entry_count FROM account_balance_ledger GROUP BY account_id) l ON l.account_id = a.id
                WHERE a.deleted_at IS NULL ORDER BY a.id
                """, (row, i) -> balance(row), settings.thresholdMicroYuan());
        var billingSummary = new LinkedHashMap<String, Object>();
        billingSummary.put("configured", true);
        billingSummary.put("price_per_million_tokens_yuan", settings.pricePerMillionTokensYuan());
        billingSummary.put("starting_balance_yuan", settings.startingBalanceYuan());
        billingSummary.put("low_balance_threshold_yuan", settings.publicView().get("low_balance_threshold_yuan"));
        billingSummary.put("account_count", balances.size());
        for (String state : List.of("balance", "low_balance", "suspended")) {
            billingSummary.put((state.equals("balance") ? "healthy" : state) + "_account_count",
                    balances.stream().filter(b -> state.equals(b.get("state"))).count());
        }
        for (String field : List.of("balance_micro_yuan", "recharge_micro_yuan", "consumed_micro_yuan", "ledger_entry_count")) {
            String source = field.equals("balance_micro_yuan") || field.equals("ledger_entry_count") ? field : "total_" + field;
            billingSummary.put("total_" + field, balances.stream().mapToLong(b -> ((Number) b.get(source)).longValue()).sum());
        }
        return Map.of("summary", totals, "by_account", byAccount, "tool_calls_by_account", tools,
                "billing", Map.of("settings", settings.publicView(), "summary", billingSummary, "by_account", balances),
                "page_size", PAGE_SIZE, "max_pages", MAX_PAGES);
    }

    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public UsagePage events(Long accountId, Long candidateId, String sessionId, int limit, long offset) {
        if (limit < 1 || limit > PAGE_SIZE || offset < 0) {
            throw new AuthException("INVALID_REQUEST", "分页参数无效。", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        var conditions = new ArrayList<String>();
        var args = new ArrayList<Object>();
        if (accountId != null) { conditions.add("account_id = ?"); args.add(accountId); }
        if (candidateId != null) { conditions.add("candidate_id = ?"); args.add(candidateId); }
        if (sessionId != null) { conditions.add("session_id = ?"); args.add(sessionId); }
        String where = conditions.isEmpty() ? "" : " WHERE " + String.join(" AND ", conditions);
        long total = jdbc.queryForObject(WINDOW + " SELECT COUNT(*) FROM retained" + where, Long.class, args.toArray());
        args.add(limit);
        args.add(offset);
        var rows = jdbc.query(WINDOW + " SELECT * FROM retained" + where + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (row, i) -> event(row), args.toArray());
        return new UsagePage(rows, total, limit, offset, PAGE_SIZE, MAX_PAGES);
    }

    private Map<String, Object> balance(ResultSet row) throws SQLException {
        var view = new LinkedHashMap<String, Object>();
        for (String key : List.of("account_id", "balance_micro_yuan", "total_recharge_micro_yuan", "total_consumed_micro_yuan",
                "low_balance_threshold_micro_yuan", "ledger_entry_count")) view.put(key, row.getLong(key));
        for (String key : List.of("email", "role", "status")) view.put(key, row.getString(key));
        long amount = row.getLong("balance_micro_yuan");
        String state = !"active".equals(row.getString("status")) || amount <= 0 ? "suspended"
                : amount <= row.getLong("low_balance_threshold_micro_yuan") ? "low_balance" : "balance";
        view.put("state", state);
        view.put("state_label", switch (state) { case "balance" -> "余额"; case "low_balance" -> "低余额"; default -> "停用"; });
        return view;
    }

    private UsageEvent event(ResultSet row) throws SQLException {
        OffsetDateTime time = row.getObject("created_at", OffsetDateTime.class).withOffsetSameInstant(ZoneOffset.UTC);
        String created = time.format(DateTimeFormatter.ofPattern(time.getNano() == 0
                ? "uuuu-MM-dd'T'HH:mm:ssxxx" : "uuuu-MM-dd'T'HH:mm:ss.SSSSSSxxx"));
        String raw = row.getString("raw_usage_json");
        Map<String, Object> usage = raw == null ? Map.of() : mapper.readValue(raw, new TypeReference<Map<String, Object>>() { });
        return new UsageEvent(row.getLong("id"), row.getLong("account_id"), nullableLong(row, "candidate_id"),
                row.getString("session_id"), row.getString("root_request_id"), row.getString("call_id"), row.getString("provider"),
                row.getString("model"), row.getString("operation"), row.getLong("input_tokens"), row.getLong("output_tokens"),
                row.getLong("total_tokens"), row.getString("usage_source"), row.getString("status"), row.getInt("attempt"),
                row.getString("provider_request_id"), usage == null ? Map.of() : usage, created, row.getBoolean("billable"), row.getString("pricing_version"));
    }

    private static Long nullableLong(ResultSet row, String column) throws SQLException {
        long value = row.getLong(column);
        return row.wasNull() ? null : value;
    }

    public record UsageEvent(long id, long account_id, Long candidate_id, String session_id, String root_request_id,
            String call_id, String provider, String model, String operation, long input_tokens, long output_tokens,
            long total_tokens, String usage_source, String status, int attempt, String provider_request_id,
            Map<String, Object> raw_usage, String created_at, boolean billable, String pricing_version) { }
    public record UsagePage(List<UsageEvent> events, long total, int limit, long offset, int page_size, int max_pages) { }
}
