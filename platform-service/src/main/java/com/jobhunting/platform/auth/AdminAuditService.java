package com.jobhunting.platform.auth;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.List;
import java.util.Map;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.core.type.TypeReference;
import tools.jackson.databind.ObjectMapper;

@Service
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class AdminAuditService {
    private final AccountAdministrationService accounts;
    private final JdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public AdminAuditService(AccountAdministrationService accounts, JdbcTemplate jdbc, ObjectMapper mapper) {
        this.accounts = accounts;
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    @Transactional
    public AuditPage page(String sessionToken, int limit, long offset) {
        accounts.authorizeAdministrator(sessionToken);
        if (limit < 1 || limit > 200 || offset < 0) {
            throw new AuthException("INVALID_REQUEST", "分页参数无效。", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        var events = jdbc.query("""
                SELECT id, actor_account_id, target_account_id, action, target_type, target_id,
                       outcome, summary, details_json, request_id, created_at
                FROM admin_audit_events ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
                """, (row, index) -> new AuditEvent(row.getLong("id"), nullableLong(row, "actor_account_id"),
                nullableLong(row, "target_account_id"), row.getString("action"), row.getString("target_type"),
                row.getString("target_id"), row.getString("outcome"), row.getString("summary"),
                mapper.readValue(row.getString("details_json"), new TypeReference<Map<String, Object>>() { }),
                row.getString("request_id"), timestamp(row)), limit, offset);
        return new AuditPage(events, limit, offset);
    }

    private static Long nullableLong(ResultSet row, String column) throws SQLException {
        long value = row.getLong(column);
        return row.wasNull() ? null : value;
    }

    private static String timestamp(ResultSet row) throws SQLException {
        OffsetDateTime time = row.getObject("created_at", OffsetDateTime.class).withOffsetSameInstant(ZoneOffset.UTC);
        return time.format(DateTimeFormatter.ofPattern(time.getNano() == 0
                ? "uuuu-MM-dd'T'HH:mm:ssxxx" : "uuuu-MM-dd'T'HH:mm:ss.SSSSSSxxx"));
    }

    public record AuditEvent(long id, Long actor_account_id, Long target_account_id, String action,
            String target_type, String target_id, String outcome, String summary, Map<String, Object> details,
            String request_id, String created_at) { }
    public record AuditPage(List<AuditEvent> events, int limit, long offset) { }
}
