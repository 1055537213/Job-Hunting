package com.jobhunting.platform.auth;

import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/** Durable intent survives Python/broker outages; only the cleanup Worker completes it. */
@Service
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class AccountDeletionRequests {
    private final JdbcTemplate jdbc;
    public AccountDeletionRequests(JdbcTemplate jdbc) { this.jdbc = jdbc; }

    public Request forAccount(long accountId) {
        return jdbc.queryForObject("SELECT account_id,request_key,task_key,request_id FROM platform_account_deletion_requests WHERE account_id=?",
                (row, index) -> new Request(row.getLong("account_id"), row.getString("request_key"), row.getString("task_key"), row.getString("request_id")), accountId);
    }

    @Transactional
    public List<Request> due(int limit) {
        // A short lease gives concurrent pollers disjoint batches, without holding a DB
        // transaction over HTTP/Redis. Unacknowledged registration/publication is retried.
        var requests = jdbc.query("""
                SELECT d.account_id,d.request_key,d.task_key,d.request_id
                FROM platform_account_deletion_requests d JOIN accounts a ON a.id=d.account_id
                WHERE d.completed_at IS NULL AND d.next_dispatch_at<=CURRENT_TIMESTAMP
                  AND a.status='disabled' AND a.deleted_at IS NULL AND a.role='user'
                ORDER BY d.next_dispatch_at,d.account_id LIMIT ? FOR UPDATE OF d SKIP LOCKED
                """, (row, index) -> new Request(row.getLong("account_id"), row.getString("request_key"),
                    row.getString("task_key"), row.getString("request_id")), limit);
        for (var request : requests) {
            jdbc.update("UPDATE platform_account_deletion_requests SET next_dispatch_at=CURRENT_TIMESTAMP+INTERVAL '60 seconds' WHERE account_id=?",
                    request.account_id());
        }
        return requests;
    }

    public record Request(long account_id, String request_key, String task_key, String request_id) { }
}
