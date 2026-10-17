package com.jobhunting.platform.tasks;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.UUID;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.AuthException;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;

@Service
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class TaskCommandService {
    private final AccountAdministrationService accounts;
    private final TaskCommandClient client;

    public TaskCommandService(AccountAdministrationService accounts, TaskCommandClient client) {
        this.accounts = accounts;
        this.client = client;
    }

    public JsonNode submit(String session, String operation, String taskKey, String idempotencyKey, String requestId) {
        var actor = accounts.authorizeAdministrator(session);
        if (!("probe".equals(operation) && taskKey == null)
                && !("retry".equals(operation) && taskKey != null && !taskKey.isBlank() && taskKey.length() <= 64)) {
            throw new AuthException("INVALID_REQUEST", "无效的任务操作。", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        String key = idempotencyKey == null ? UUID.randomUUID().toString() : idempotencyKey;
        if (!key.matches("[A-Za-z0-9._-]{8,128}")) {
            throw new AuthException("INVALID_REQUEST", "幂等键格式无效。", HttpStatus.UNPROCESSABLE_ENTITY);
        }
        return client.submit(commandId(actor.account_id(), key), operation, taskKey, session, requestId);
    }

    private static String commandId(long actorId, String key) {
        try {
            // Intent is deliberately excluded: Python must detect reuse for a different operation or task.
            byte[] input = (actorId + ":" + key).getBytes(StandardCharsets.UTF_8);
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(input));
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException("SHA-256 is unavailable");
        }
    }
}
