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
    volatile int delayMs;
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
            try { Thread.sleep(delayMs); } catch (InterruptedException error) { Thread.currentThread().interrupt(); }
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", contentType);
            exchange.getResponseHeaders().set("Location", "/redirect");
            exchange.sendResponseHeaders(status, bytes.length);
            try { exchange.getResponseBody().write(bytes); } catch (java.io.IOException ignored) { } finally { exchange.close(); }
        });
        server.start();
        client = new ChatSessionClient(mapper, "http://127.0.0.1:" + server.getAddress().getPort(), "service-secret", "test", "http://localhost:8000", 300000);
    }
    @AfterEach void close() { if (server != null) server.stop(0); }
    static ObjectNode reply() {
        return (ObjectNode) new ObjectMapper().readTree("""
            {"mode":"langchain_agent","reply":"Done","display_reply":"Done","task_trace":null,"background_tasks":[],
             "profile":{"id":7,"name":"Owner","status":"student","education":"bachelor","experience_years":0,
              "skills":{},"preferred_cities":[],"acceptable_cities":[],"salary_floor_k":null,"expected_salary_k":null,
              "target_directions":[],"unacceptable":[],"preference_weights":{}}}
            """);
    }
    @Test void invocationOnlyReturnsPublicReplyBoundToOwnerSessionAndProfile() {
        var args = mapper.createObjectNode().put("candidate_id",7).put("message","Hello").put("session_id","chat-1");
        var receipt = mapper.createObjectNode().put("account_id",42).put("session_id","chat-1");
        receipt.set("result",reply()); body = receipt.toString();
        assertThat(client.invoke("opaque-session",42,args,"message-trace")).isEqualTo(reply());
        assertThat(requests.getFirst()).contains("/internal/v1/chat/invoke", "message-trace", "opaque-session").doesNotContain("account_id");
        assertThat(client.invoke("opaque-session",42,args,"message-trace")).isEqualTo(reply());
        String first = mapper.readTree(requests.get(0).substring(requests.get(0).indexOf('{'))).get("execution_id").asText();
        String second = mapper.readTree(requests.get(1).substring(requests.get(1).indexOf('{'))).get("execution_id").asText();
        assertThat(first).matches("[a-f0-9]{32}").isNotEqualTo(second);
        for (String corrupt : List.of("owner","session","profile","tools","metadata","trace")) {
            receipt.put("account_id",42).put("session_id","chat-1"); var result = reply(); receipt.set("result",result);
            switch (corrupt) {
                case "owner" -> receipt.put("account_id",99);
                case "session" -> receipt.put("session_id","chat-other");
                case "profile" -> ((ObjectNode)result.get("profile")).put("id",99);
                case "tools" -> result.put("tool_outputs","private");
                case "metadata" -> ((ObjectNode)result.get("profile")).put("token_hash","private");
                default -> result.putObject("task_trace").put("tool_output","private");
            }
            body = receipt.toString(); int previous = requests.size();
            assertThatThrownBy(() -> client.invoke("opaque-session",42,args,"message-trace"))
                .isInstanceOfSatisfying(AuthException.class,e -> assertThat(e.code()).isEqualTo("CHAT_EXECUTION_UNCONFIRMED"));
            assertThat(requests).hasSize(previous+1);
        }
    }
    @Test void invocationPreservesSafeBalanceErrorAndNeverRetriesOrLeaksModelErrors() {
        var args = mapper.createObjectNode().put("candidate_id",7).put("message","Hello");
        status = 402; body = "{\"code\":\"INSUFFICIENT_BALANCE\",\"detail\":\"private-url\"}";
        assertThatThrownBy(() -> client.invoke("opaque-session",42,args,"message-trace"))
            .isInstanceOfSatisfying(AuthException.class,e -> {
                assertThat(e.code()).isEqualTo("INSUFFICIENT_BALANCE"); assertThat(e.getMessage()).isEqualTo("余额不足，请先充值后重试。");
            });
        status = 502; body = "{\"code\":\"RAW_MODEL_ERROR\",\"detail\":\"private-url\"}";
        assertThatThrownBy(() -> client.invoke("opaque-session",42,args,"message-trace"))
            .isInstanceOfSatisfying(AuthException.class,e -> assertThat(e.code()).isEqualTo("CHAT_EXECUTION_UNCONFIRMED"));
        assertThat(requests).hasSize(2);
        status = 200;
        for (String invalid : List.of("{\"account_id\":42,\"account_id\":42}", "{\"reply\":\""+"x".repeat(ChatSessionClient.MAX_BYTES)+"\"}")) {
            body=invalid;
            assertThatThrownBy(() -> client.invoke("opaque-session",42,args,"message-trace"))
                .isInstanceOfSatisfying(AuthException.class,e -> assertThat(e.code()).isEqualTo("CHAT_EXECUTION_UNCONFIRMED"));
        }
    }
    @Test void invocationDeadlineCancelsTransportWithoutReplayAndReleasesAdmissionSlot() throws Exception {
        client = new ChatSessionClient(mapper, "http://127.0.0.1:"+server.getAddress().getPort(), "service-secret",
            "test", "http://localhost:8000", 500);
        var args = mapper.createObjectNode().put("candidate_id",7).put("message","Hello");
        body = mapper.createObjectNode().put("account_id",42).put("session_id","account-42-candidate-7").set("result",reply()).toString();
        delayMs = 1500;
        long started = System.nanoTime();
        assertThatThrownBy(() -> client.invoke("opaque-session",42,args,"message-trace"))
            .isInstanceOfSatisfying(AuthException.class,e -> assertThat(e.code()).isEqualTo("CHAT_EXECUTION_UNCONFIRMED"));
        assertThat((System.nanoTime()-started)/1_000_000).isLessThan(2500);
        Thread.sleep(1600); delayMs=0;
        assertThat(requests).hasSize(1);
        assertThat(client.invoke("opaque-session",42,args,"message-trace")).isEqualTo(reply());
        assertThat(requests).hasSize(2);
    }
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
            assertThatThrownBy(() -> new ChatSessionClient(mapper, url, "strong-".repeat(8), "production", "https://example.com", 300000)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new ChatSessionClient(mapper, "http://web:8000", "weak", "production", "https://example.com", 300000)).isInstanceOf(IllegalArgumentException.class);
    }
}
