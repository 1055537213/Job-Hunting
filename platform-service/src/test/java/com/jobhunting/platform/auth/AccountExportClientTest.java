package com.jobhunting.platform.auth;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.CompletableFuture;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

import static org.assertj.core.api.Assertions.*;

class AccountExportClientTest {
    final ObjectMapper mapper = new ObjectMapper();
    final String token = "service-token-012345678901234567890123";
    final List<String> requests = new CopyOnWriteArrayList<>();
    HttpServer server;
    ExecutorService executor;
    String origin;
    volatile String body;
    volatile String contentType = "application/json";
    volatile int status = 200;
    volatile int delay;
    volatile boolean stall;

    @BeforeEach void setup() throws IOException {
        body = fixture().toString();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        origin = "http://127.0.0.1:" + server.getAddress().getPort();
        executor = Executors.newCachedThreadPool();
        server.setExecutor(executor);
        server.createContext("/", exchange -> {
            requests.add(exchange.getRequestMethod() + " " + exchange.getRequestURI() + " "
                    + exchange.getRequestHeaders().getFirst("X-Internal-Service-Token") + " "
                    + new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            try {
                String captured = body;
                if (!stall && delay > 0) Thread.sleep(delay);
                byte[] bytes = captured.getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().set("Content-Type", contentType);
                exchange.getResponseHeaders().set("Location", origin + "/redirect");
                exchange.sendResponseHeaders(status, bytes.length);
                if (stall) { exchange.getResponseBody().flush(); Thread.sleep(delay); }
                exchange.getResponseBody().write(bytes);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            } catch (IOException closed) {
                // Deliberate timeout/size cancellation closes the response.
            } finally { exchange.close(); }
        });
        server.start();
    }

    @AfterEach void close() { server.stop(0); executor.shutdownNow(); }

    @Test void postsRealSessionAndServiceCredentialAndPreservesUserText() {
        var data = fixture();
        data.withArray("jobs").add(mapper.createObjectNode().put("id", 1).put("account_id", 42).put("text", "password_hash 是用户文本"));
        body = data.toString();
        assertThat(client(1000).export("real-session", 42)).isEqualTo(data);
        assertThat(requests).hasSize(1);
        assertThat(requests.getFirst()).isEqualTo("POST /internal/v1/accounts/export " + token + " {\"session_token\":\"real-session\"}");
    }

    @ParameterizedTest @ValueSource(strings = {"owner", "balance", "secret", "parent", "extra", "consent", "account"})
    void rejectsInvalidOrCrossTenantProjection(String invalid) {
        var data = fixture();
        switch (invalid) {
            case "owner" -> data.withArray("jobs").add(mapper.createObjectNode().put("account_id", 43));
            case "balance" -> ((ObjectNode) data.get("account_balance")).put("account_id", 43);
            case "secret" -> data.withArray("jobs").add(mapper.createObjectNode().put("account_id", 42).put("token_hash", "secret"));
            case "parent" -> data.withArray("knowledge_asset_versions").add(mapper.createObjectNode().put("asset_id", 999));
            case "extra" -> data.put("internal_token", token);
            case "consent" -> data.withArray("consents").add(mapper.createObjectNode().put("secret", "secret"));
            case "account" -> ((ObjectNode) data.get("account")).put("id", 43);
        }
        body = data.toString();
        assertUnavailable(client(1000));
    }

    @Test void duplicateFieldsTrailingJsonAndWrongContentTypeFailClosed() {
        var client = client(1000);
        for (String invalid : List.of("null", "[]", "{}", "{", fixture() + " {}",
                fixture().toString().replace("\"id\":42", "\"id\":42,\"id\":42"))) {
            body = invalid;
            assertUnavailable(client);
        }
        body = fixture().toString();
        contentType = "text/html";
        assertUnavailable(client);
    }

    @ParameterizedTest @ValueSource(ints = {301, 302, 403, 429, 500, 503})
    void upstreamFailuresNeverReflectBodyOrFollowRedirects(int failure) {
        status = failure;
        body = "raw-secret " + token;
        assertUnavailable(client(1000));
        assertThat(requests).hasSize(1);
    }

    @ParameterizedTest @ValueSource(ints = {401, 413})
    void preservesOnlySafeBusinessErrors(int failure) {
        status = failure;
        body = "raw-secret";
        assertThatThrownBy(() -> client(1000).export("real-session", 42)).isInstanceOfSatisfying(AuthException.class,
                error -> assertThat(error.status().value()).isEqualTo(failure)).hasMessageNotContaining("raw-secret");
    }

    @Test void bodySizeAndTotalDeadlineAreBoundedAndSlotsRecover() {
        body = " ".repeat(AccountExportClient.MAX_BYTES + 1);
        assertUnavailable(client(2000));
        body = fixture().toString();
        var client = client(100);
        delay = 600;
        assertThatCode(() -> assertUnavailable(client)).doesNotThrowAnyException();
        stall = true;
        assertThatCode(() -> assertUnavailable(client)).doesNotThrowAnyException();
        delay = 0;
        stall = false;
        assertThat(client.export("real-session", 42).path("account").path("id").asInt()).isEqualTo(42);
    }

    @Test void validatesProductionOriginTokenAndTimeoutsWithoutReflectingConfiguration() {
        for (String url : List.of("https://public.example.com", "http://8.8.8.8", "http://user:secret@web:8000", "http://web:8000/api", "http://web:8000?secret=x")) {
            assertThatThrownBy(() -> new AccountExportClient(mapper, url, token, 1000, 30000, "production", "https://public.example.com"))
                    .isInstanceOf(IllegalArgumentException.class).hasMessageNotContaining("secret");
        }
        assertThatCode(() -> new AccountExportClient(mapper, "http://web:8000", token, 1000, 30000, "production", "https://public.example.com"))
                .doesNotThrowAnyException();
        assertThatThrownBy(() -> new AccountExportClient(mapper, origin, "short", 1000, 30000, "production", "https://public.example.com"))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> client(30001)).isInstanceOf(IllegalArgumentException.class);
    }

    @Test void rejectsThirdConcurrentExportAndReleasesSlots() throws Exception {
        delay = 500;
        var client = client(2000);
        var first = CompletableFuture.supplyAsync(() -> client.export("real-session", 42));
        var second = CompletableFuture.supplyAsync(() -> client.export("real-session", 42));
        try {
            long deadline = System.nanoTime() + Duration.ofSeconds(2).toNanos();
            while (requests.size() < 2 && System.nanoTime() < deadline) Thread.sleep(5);
            assertThat(requests).hasSize(2);
            assertUnavailable(client);
        } finally { first.join(); second.join(); }
        delay = 0;
        assertThat(client.export("real-session", 42).path("account").path("id").asLong()).isEqualTo(42);
    }

    private void assertUnavailable(AccountExportClient client) {
        assertTimeoutPreemptively(Duration.ofSeconds(3), () ->
                assertThatThrownBy(() -> client.export("real-session", 42)).isInstanceOfSatisfying(AuthException.class,
                        error -> assertThat(error.status().value()).isEqualTo(503)).hasMessageNotContaining(token).hasMessageNotContaining("raw-secret"));
    }

    private void assertTimeoutPreemptively(Duration duration, org.junit.jupiter.api.function.Executable action) {
        org.junit.jupiter.api.Assertions.assertTimeoutPreemptively(duration, action);
    }

    private AccountExportClient client(int timeout) {
        return new AccountExportClient(mapper, origin, token, 1000, timeout, "test", "http://localhost:8000");
    }

    ObjectNode fixture() {
        var data = mapper.createObjectNode().put("exported_at", "2026-10-17T00:00:00Z");
        data.set("account", mapper.createObjectNode().put("id", 42).put("email", "owner@example.com")
                .put("display_name", "Owner").put("role", "user").put("status", "active").put("must_change_password", false)
                .putNull("email_verified_at").putNull("deleted_at").put("created_at", "now").put("updated_at", "now"));
        data.putArray("consents");
        data.set("account_balance", mapper.createObjectNode().put("account_id", 42));
        for (String table : AccountExportClient.DIRECT) data.putArray(table);
        for (String table : List.of("knowledge_asset_versions", "project_archive_files", "project_collection_files")) data.putArray(table);
        return data;
    }
}
