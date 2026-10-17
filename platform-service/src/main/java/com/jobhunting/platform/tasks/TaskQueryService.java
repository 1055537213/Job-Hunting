package com.jobhunting.platform.tasks;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
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
public class TaskQueryService {
    // Explicit projection excludes payload, idempotency keys, session IDs and internal row IDs.
    private static final String COLUMNS = """
            task_key, task_type, status, progress, attempt, max_attempts, result_json, error_summary,
            created_at, started_at, finished_at, updated_at
            """;
    private static final Set<String> STATUSES = Set.of("queued", "running", "succeeded", "failed", "cancelled");
    private final JdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public TaskQueryService(JdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public TaskPage adminList(Long accountId, String type, String status, int limit, long offset) {
        if (limit < 1 || limit > 100 || offset < 0 || (accountId != null && accountId < 1)
                || (type != null && (type.isEmpty() || type.length() > 80))
                || (status != null && (status.isEmpty() || status.length() > 20))) {
            throw new AuthException("INVALID_REQUEST", "请求参数无效。", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        if (status != null && !STATUSES.contains(status)) {
            throw new AuthException("INVALID_TASK_STATUS", "无效的后台任务状态。", HttpStatus.BAD_REQUEST);
        }
        var conditions = new ArrayList<String>();
        var args = new ArrayList<Object>();
        if (accountId != null) { conditions.add("account_id = ?"); args.add(accountId); }
        if (type != null) { conditions.add("task_type = ?"); args.add(type.strip()); }
        if (status != null) { conditions.add("status = ?"); args.add(status); }
        String where = conditions.isEmpty() ? "" : " WHERE " + String.join(" AND ", conditions);
        long total = jdbc.queryForObject("SELECT COUNT(*) FROM background_tasks" + where, Long.class, args.toArray());
        args.add(limit);
        args.add(offset);
        var tasks = jdbc.query("SELECT " + COLUMNS + ", account_id, candidate_id FROM background_tasks"
                + where + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?", (row, i) -> {
                    var task = project(row);
                    task.put("account_id", row.getLong("account_id"));
                    long candidate = row.getLong("candidate_id");
                    task.put("candidate_id", row.wasNull() ? null : candidate);
                    return task;
                }, args.toArray());
        // max_pages is legacy presentation metadata, not a background-task deletion policy.
        return new TaskPage(tasks, total, limit, offset, 100, 5);
    }

    @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
    public Map<String, Object> personal(long accountId, String key) {
        if (key == null || key.isBlank() || key.length() > 64) {
            throw new AuthException("INVALID_REQUEST", "任务标识无效。", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        var tasks = jdbc.query("SELECT " + COLUMNS + " FROM background_tasks WHERE task_key = ? AND account_id = ?",
                (row, i) -> project(row), key, accountId);
        if (tasks.isEmpty()) throw new AuthException("TASK_NOT_FOUND", "后台任务不存在。", HttpStatus.NOT_FOUND);
        return Map.of("task", tasks.getFirst());
    }

    private Map<String, Object> project(ResultSet row) throws SQLException {
        var task = new LinkedHashMap<String, Object>();
        for (String field : List.of("task_key", "task_type", "status", "error_summary")) task.put(field, row.getString(field));
        for (String field : List.of("progress", "attempt", "max_attempts")) task.put(field, row.getInt(field));
        String raw = row.getString("result_json");
        var json = raw == null ? null : mapper.readTree(raw);
        Map<String, Object> result = json != null && json.isObject()
                ? mapper.convertValue(json, new TypeReference<Map<String, Object>>() { }) : Map.of();
        task.put("result", result);
        for (String field : List.of("created_at", "started_at", "finished_at", "updated_at")) {
            OffsetDateTime time = row.getObject(field, OffsetDateTime.class);
            task.put(field, time == null ? null : time.withOffsetSameInstant(ZoneOffset.UTC).format(DateTimeFormatter.ofPattern(
                    time.getNano() == 0 ? "uuuu-MM-dd'T'HH:mm:ssxxx" : "uuuu-MM-dd'T'HH:mm:ss.SSSSSSxxx")));
        }
        return task;
    }

    public record TaskPage(List<Map<String, Object>> tasks, long total, int limit, long offset, int page_size, int max_pages) { }
}
