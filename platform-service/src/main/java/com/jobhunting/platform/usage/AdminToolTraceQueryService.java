package com.jobhunting.platform.usage;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import com.jobhunting.platform.auth.AuthException;
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
public class AdminToolTraceQueryService {
    // Do not load trace_json in the window or list query; details can be large.
    private static final String WINDOW = """
            WITH ranked AS (SELECT id, account_id, ROW_NUMBER() OVER
              (PARTITION BY account_id ORDER BY updated_at DESC, id DESC) AS position FROM tool_call_traces),
            retained AS (SELECT id, account_id FROM ranked WHERE position <= 500)
            """;
    private static final String COLUMNS = """
            t.id, t.account_id, t.candidate_id, t.session_id, t.root_request_id, t.title, t.status,
            t.source, t.step_count, t.attempt_count, t.last_step_name, t.last_error_summary,
            t.created_at, t.started_at, t.finished_at, t.updated_at
            """;
    private final JdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public AdminToolTraceQueryService(JdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public TracePage list(Long accountId, int limit, long offset) {
        if (limit < 1 || limit > AdminUsageQueryService.PAGE_SIZE || offset < 0) {
            throw new AuthException("INVALID_REQUEST", "分页参数无效。", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        String where = accountId == null ? "" : " WHERE r.account_id = ?";
        Object[] filter = accountId == null ? new Object[]{} : new Object[]{accountId};
        long total = jdbc.queryForObject(WINDOW + " SELECT COUNT(*) FROM retained r" + where, Long.class, filter);
        Object[] page = accountId == null ? new Object[]{limit, offset} : new Object[]{accountId, limit, offset};
        var traces = jdbc.query(WINDOW + " SELECT " + COLUMNS + " FROM tool_call_traces t JOIN retained r ON r.id = t.id"
                + where + " ORDER BY t.updated_at DESC, t.id DESC LIMIT ? OFFSET ?", (row, i) -> summary(row), page);
        return new TracePage(traces, total, limit, offset, AdminUsageQueryService.PAGE_SIZE, AdminUsageQueryService.MAX_PAGES);
    }

    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public Map<String, Object> detail(String rootRequestId) {
        if (rootRequestId == null || rootRequestId.isBlank() || rootRequestId.length() > 128) {
            throw new AuthException("INVALID_REQUEST", "任务标识无效。", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        // Rank every task for the account before filtering by root ID, just like list retention.
        var traces = jdbc.query(WINDOW + " SELECT " + COLUMNS + ", t.trace_json FROM tool_call_traces t JOIN retained r ON r.id = t.id"
                + " WHERE t.root_request_id = ?", (row, i) -> {
                    var result = summary(row);
                    String raw = row.getString("trace_json");
                    var json = raw == null ? null : mapper.readTree(raw);
                    Map<String, Object> trace = json != null && json.isObject()
                            ? mapper.convertValue(json, new TypeReference<Map<String, Object>>() { }) : Map.of();
                    result.put("trace", trace);
                    return result;
                }, rootRequestId);
        if (traces.isEmpty()) throw new AuthException("TRACE_NOT_FOUND", "工具调用记录不存在。", HttpStatus.NOT_FOUND);
        return Map.of("trace", traces.getFirst());
    }

    private Map<String, Object> summary(ResultSet row) throws SQLException {
        var result = new LinkedHashMap<String, Object>();
        result.put("id", row.getLong("id"));
        result.put("account_id", row.getLong("account_id"));
        long candidate = row.getLong("candidate_id");
        result.put("candidate_id", row.wasNull() ? null : candidate);
        for (String key : List.of("session_id", "root_request_id", "title", "status", "source", "last_step_name", "last_error_summary")) {
            result.put(key, row.getString(key));
        }
        result.put("step_count", row.getInt("step_count"));
        result.put("attempt_count", row.getInt("attempt_count"));
        for (String key : List.of("created_at", "started_at", "finished_at", "updated_at")) {
            OffsetDateTime time = row.getObject(key, OffsetDateTime.class);
            result.put(key, time == null ? null : time.withOffsetSameInstant(ZoneOffset.UTC).format(
                    DateTimeFormatter.ofPattern(time.getNano() == 0 ? "uuuu-MM-dd'T'HH:mm:ssxxx" : "uuuu-MM-dd'T'HH:mm:ss.SSSSSSxxx")));
        }
        return result;
    }

    public record TracePage(List<Map<String, Object>> traces, long total, int limit, long offset, int page_size, int max_pages) { }
}
