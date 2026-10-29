package com.jobhunting.platform.jobs;

import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicInteger;
import com.jobhunting.platform.auth.AuthException;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class JobManagementClientTest {
    final ObjectMapper mapper = new ObjectMapper();
    final AtomicInteger calls = new AtomicInteger();
    HttpServer server;
    JobManagementClient client;
    String response = "{\"account_id\":42,\"jobs\":[]}";
    int status = 200;

    @BeforeEach void setup() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/internal/v1/jobs/", exchange -> {
            calls.incrementAndGet();
            var body = mapper.readTree(exchange.getRequestBody().readAllBytes());
            assertThat(body.path("session_token").asText()).isEqualTo("real-session");
            assertThat(body.has("account_id")).isFalse();
            assertThat(exchange.getRequestHeaders().getFirst("X-Internal-Service-Token")).isEqualTo("secret");
            byte[] bytes = response.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(status, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        server.start();
        client = new JobManagementClient(mapper, "http://127.0.0.1:" + server.getAddress().getPort(),
                "secret", "development", "http://localhost:8000");
    }

    @AfterEach void close() { server.stop(0); }

    @Test void validatesOwnerAndAllowlistedDeleteProjection() {
        assertThat(client.list("real-session", 42, "trace").toString()).isEqualTo("{\"jobs\":[]}");
        response = "{\"account_id\":42,\"deleted\":true,\"job_id\":7}";
        var result = client.delete("real-session", 42, 7, "trace");
        assertThat(result.path("deleted").asBoolean()).isTrue();
        assertThat(result.has("account_id")).isFalse();
    }

    @Test void rejectsForeignOrPrivateResponsesAndRedactsErrors() {
        response = "{\"account_id\":99,\"jobs\":[]}";
        assertThatThrownBy(() -> client.list("real-session", 42, "trace")).isInstanceOf(AuthException.class);
        response = "{\"account_id\":42,\"deleted\":true,\"job_id\":7,\"storage_keys\":[\"private\"]}";
        assertThatThrownBy(() -> client.delete("real-session", 42, 7, "trace")).isInstanceOf(AuthException.class);
        status = 500;
        response = "{\"code\":\"INTERNAL\",\"detail\":\"private credential\"}";
        assertThatThrownBy(() -> client.list("real-session", 42, "trace"))
                .isInstanceOf(AuthException.class).hasMessageNotContaining("private");
    }

    @Test void rejectsMissingTokenBeforeCallingPython() {
        var isolated = new JobManagementClient(mapper, "http://127.0.0.1:" + server.getAddress().getPort(),
                "", "development", "http://localhost:8000");
        assertThatThrownBy(() -> isolated.list("real-session", 42, "trace")).isInstanceOf(AuthException.class);
        assertThat(calls.get()).isZero();
    }
}
