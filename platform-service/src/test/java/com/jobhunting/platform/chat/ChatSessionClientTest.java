package com.jobhunting.platform.chat;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import com.sun.net.httpserver.HttpServer;
import com.jobhunting.platform.auth.AuthException;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;
import static org.assertj.core.api.Assertions.*;

class ChatSessionClientTest {
    final ObjectMapper mapper = new ObjectMapper();
    final List<String> requests = new CopyOnWriteArrayList<>();
    HttpServer server;
    ChatSessionClient client;
    String body;
    int status = 200;
    String contentType = "application/json";
    static ObjectNode session() {
        return (ObjectNode) new ObjectMapper().readTree("""
            {"id":1,"account_id":42,"candidate_id":7,"session_id":"chat-1","job_id":null,"title":"Interview",
             "status":"active","created_at":"now","updated_at":"now","archived_at":null}
            """);
    }
    @BeforeEach void setup() throws Exception {
        body = mapper.createObjectNode().put("account_id", 42).set("sessions", mapper.createArrayNode().add(session())).toString();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            requests.add(exchange.getRequestURI() + " " + exchange.getRequestHeaders().getFirst("X-Trace-Id") + " "
                    + new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", contentType);
            exchange.getResponseHeaders().set("Location", "/redirect");
            exchange.sendResponseHeaders(status, bytes.length);
            try { exchange.getResponseBody().write(bytes); } catch (java.io.IOException ignored) { } finally { exchange.close(); }
        });
        server.start();
        client = new ChatSessionClient(mapper, "http://127.0.0.1:" + server.getAddress().getPort(), "service-secret", "test", "http://localhost:8000");
    }
    @AfterEach void close() { if (server != null) server.stop(0); }
    @Test void requestUsesRealSessionAndTraceAndResponseIsBoundToOwnerAndCandidate() {
        var args = mapper.createObjectNode().put("candidate_id", 7);
        assertThat(client.execute("list", "opaque-session", 42, args, "chat-trace").get("sessions").size()).isEqualTo(1);
        assertThat(requests.getFirst()).contains("/internal/v1/chat/sessions/list", "chat-trace", "opaque-session").doesNotContain("account_id");
        for (String corrupt : List.of("owner", "candidate", "secret", "duplicate", "status", "root")) {
            var receipt = mapper.createObjectNode().put("account_id", 42);
            var row = session(); var rows = receipt.putArray("sessions").add(row);
            switch (corrupt) {
                case "owner" -> row.put("account_id", 99);
                case "candidate" -> row.put("candidate_id", 99);
                case "secret" -> row.put("token_hash", "private-value");
                case "duplicate" -> rows.add(row.deepCopy());
                case "status" -> row.put("status", "archived");
                default -> receipt.put("private", "value");
            }
            body = receipt.toString();
            unavailable(args);
        }
    }
    @Test void historyNeverAcceptsWrongSessionMessageOrRawInternalMetadata() {
        var args = mapper.createObjectNode().put("candidate_id", 7).put("session_id", "chat-1").put("limit", 1);
        var receipt = mapper.createObjectNode().put("account_id", 42).put("candidate_id", 7).put("session_id", "chat-1");
        var message = (ObjectNode) mapper.readTree("""
            {"id":1,"candidate_id":7,"session_id":"chat-1","role":"assistant","content":"Done","metadata":{},"created_at":"now"}
            """);
        receipt.putArray("messages").add(message); body = receipt.toString();
        assertThat(client.execute("history", "opaque-session", 42, args, "trace").get("messages").size()).isEqualTo(1);
        message.put("session_id", "chat-other"); body = receipt.toString();
        assertThatThrownBy(() -> client.execute("history", "opaque-session", 42, args, "trace")).isInstanceOf(AuthException.class);
        message.put("session_id", "chat-1"); ((ObjectNode) message.get("metadata")).put("tool_outputs", "private"); body = receipt.toString();
        assertThatThrownBy(() -> client.execute("history", "opaque-session", 42, args, "trace")).isInstanceOf(AuthException.class);
    }
    @Test void redirectsInvalidJsonUnknownErrorsAndOversizeFailClosedWithoutReplay() {
        var args = mapper.createObjectNode();
        for (String invalid : List.of("redirect", "duplicate", "mime", "oversize", "wrong-error")) {
            status = 200; contentType = "application/json";
            switch (invalid) {
                case "redirect" -> { status = 302; body = "{}"; }
                case "duplicate" -> body = "{\"account_id\":42,\"sessions\":[],\"sessions\":[]}";
                case "mime" -> { contentType = "text/plain"; body = "{}"; }
                case "oversize" -> body = " ".repeat(ChatSessionClient.MAX_BYTES + 1);
                default -> { status = 401; body = "{\"code\":\"INTERNAL_UNAUTHORIZED\",\"detail\":\"private-value\"}"; }
            }
            int before = requests.size(); unavailable(args);
            assertThat(requests.size()).isEqualTo(before + 1);
        }
    }
    void unavailable(ObjectNode args) {
        assertThatThrownBy(() -> client.execute("list", "opaque-session", 42, args, "trace"))
            .isInstanceOfSatisfying(AuthException.class, e -> assertThat(e.status().value()).isEqualTo(503))
            .hasMessageNotContaining("private-value");
    }
    @Test void productionRejectsPublicCallbackAndWeakCredentials() {
        for (String url : List.of("https://example.com", "http://user:pass@web:8000", "http://web:8000/path", "http://web:8000?key=1"))
            assertThatThrownBy(() -> new ChatSessionClient(mapper, url, "strong-".repeat(8), "production", "https://example.com")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new ChatSessionClient(mapper, "http://web:8000", "weak", "production", "https://example.com")).isInstanceOf(IllegalArgumentException.class);
    }
}
