package com.jobhunting.platform.chat;

import java.util.Set;
import com.jobhunting.platform.profiles.ProfileQueryClient;
import tools.jackson.databind.JsonNode;

/** Validate the public projection, not raw model/tool output. */
final class ChatReply {
    private ChatReply() { }
    static void validate(JsonNode reply, long candidate, String requestId) {
        require(ChatSessionClient.fields(reply, Set.of("mode", "reply", "display_reply", "task_trace", "background_tasks", "profile")));
        text(reply, "mode", "reply", "display_reply");
        ProfileQueryClient.validateProfile(reply.get("profile"));
        require(reply.get("profile").get("id").asLong() == candidate);
        var trace = reply.get("task_trace");
        if (!trace.isNull()) {
            require(ChatSessionClient.fields(trace, Set.of("version", "root_request_id", "title", "status", "duration_ms", "steps", "approval")));
            text(trace, "root_request_id", "title", "status");
            require(trace.get("version").isIntegralNumber() && requestId.equals(trace.get("root_request_id").asText())
                    && (trace.get("duration_ms").isNull() || trace.get("duration_ms").isIntegralNumber()) && trace.get("steps").isArray());
            for (var step : trace.get("steps")) {
                require(ChatSessionClient.fields(step, Set.of("id", "label", "status", "summary")));
                text(step, "id", "label", "status", "summary");
            }
            var approval = trace.get("approval");
            if (!approval.isNull()) {
                require(ChatSessionClient.fields(approval, Set.of("kind", "record_id", "title", "message", "items", "confirm_label", "cancel_label", "status")));
                text(approval, "kind", "title", "message", "confirm_label", "cancel_label", "status");
                var id = approval.get("record_id");
                require((id.isNull() || id.isTextual() || id.isIntegralNumber()) && approval.get("items").isArray());
                for (var item : approval.get("items")) {
                    require(ChatSessionClient.fields(item, Set.of("label", "value"))); text(item, "label", "value");
                }
            }
        }
        require(reply.get("background_tasks").isArray());
        for (var task : reply.get("background_tasks")) {
            require(ChatSessionClient.fields(task, Set.of("task_key", "task_type", "status", "progress")) && task.get("progress").isIntegralNumber());
            text(task, "task_key", "task_type", "status");
        }
    }
    private static void text(JsonNode node, String... keys) { for (var key : keys) require(node.path(key).isTextual()); }
    private static void require(boolean valid) { if (!valid) throw ChatSessionClient.unconfirmed(); }
}
