package com.jobhunting.platform.profiles;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import com.sun.net.httpserver.HttpServer;
import com.jobhunting.platform.auth.AuthException;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

import static org.assertj.core.api.Assertions.*;

class ProfileQueryClientTest {
    final ObjectMapper mapper = new ObjectMapper();
    final String token = "service-token-012345678901234567890123";
    final List<String> requests = new CopyOnWriteArrayList<>();
    HttpServer server;
    ExecutorService executor;
    String origin;
    volatile String body;
    volatile int status = 200;
    volatile String contentType = "application/json";
    volatile int delay;
    volatile boolean stall;
    volatile CountDownLatch releaseBodies;

    @BeforeEach void setup() throws IOException {
        body = fixture(false).toString();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        origin = "http://127.0.0.1:" + server.getAddress().getPort();
        executor = Executors.newCachedThreadPool();
        server.setExecutor(executor);
        server.createContext("/", exchange -> {
            requests.add(exchange.getRequestMethod() + " " + exchange.getRequestURI() + " "
                    + exchange.getRequestHeaders().getFirst("X-Internal-Service-Token") + " "
                    + exchange.getRequestHeaders().getFirst("X-Trace-Id") + " "
                    + new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            try {
                byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
                if (releaseBodies != null) releaseBodies.await(3, TimeUnit.SECONDS);
                if (!stall && delay > 0) Thread.sleep(delay);
                exchange.getResponseHeaders().set("Content-Type", contentType);
                exchange.getResponseHeaders().set("Location", origin + "/redirect");
                exchange.sendResponseHeaders(status, bytes.length);
                if (stall) { exchange.getResponseBody().flush(); Thread.sleep(delay); }
                exchange.getResponseBody().write(bytes);
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
            } catch (IOException closed) {
                // The bounded/deadline client deliberately cancels its response.
            } finally { exchange.close(); }
        });
        server.start();
    }

    @AfterEach void close() { server.stop(0); executor.shutdownNow(); }

    static ObjectNode profile() {
        return (ObjectNode) new ObjectMapper().readTree("""
                {"id":7,"name":"Owner","status":"student","education":"bachelor","experience_years":1.5,
                 "skills":{"Python":"熟练"},"preferred_cities":["杭州"],"acceptable_cities":[],"salary_floor_k":10,
                 "expected_salary_k":null,"target_directions":["后端开发"],"unacceptable":[],"preference_weights":{"city":1.0}}
                """);
    }

    ObjectNode fixture(boolean detail) {
        var value = mapper.createObjectNode().put("account_id", 42);
        if (detail) value.set("profile", profile()); else value.putArray("profiles").add(profile());
        return value;
    }

    ProfileQueryClient client(int timeout) { return new ProfileQueryClient(mapper, origin, token, 1000, timeout, "test", "http://localhost:8000"); }
    void unavailable(ProfileQueryClient client) {
        org.junit.jupiter.api.Assertions.assertTimeoutPreemptively(Duration.ofSeconds(3), () ->
                assertThatThrownBy(() -> client.query("real-session", 42, null, "profile-query-trace"))
                        .isInstanceOfSatisfying(AuthException.class, e -> assertThat(e.status().value()).isEqualTo(503))
                        .hasMessageNotContaining(token).hasMessageNotContaining("private-upstream"));
    }

    @Test void forwardsRealSessionAndTraceNotOwnerAndReturnsOnlyPublicContract() {
        assertThat(client(2000).query("real-session", 42, null, "profile-query-trace").toString())
                .isEqualTo(mapper.createObjectNode().set("profiles", fixture(false).get("profiles")).toString());
        assertThat(requests).containsExactly("POST /internal/v1/profiles/query " + token
                + " profile-query-trace {\"session_token\":\"real-session\",\"candidate_id\":null}");
        body = fixture(true).toString();
        assertThat(client(2000).query("real-session", 42, 7L, "profile-query-trace").path("profile").path("id").asLong()).isEqualTo(7);
    }

    @Test void creationUsesSameBoundedTransportAndValidatesOwnerAndReceipt() {
        var receipt = fixture(true).put("candidate_id", 7);
        body = receipt.toString();
        var input = mapper.createObjectNode().put("name", "Candidate");
        var client = client(2000);
        assertThat(client.create("real-session", 42, "stable-intent", input, "creation-trace").get("candidate_id").asLong()).isEqualTo(7);
        assertThat(requests.getFirst()).startsWith("POST /internal/v1/profiles/create ").contains("stable-intent", "real-session");
        assertThat(requests.getFirst()).doesNotContain("account_id");
        for (String invalid : List.of("owner", "id", "secret", "profile")) {
            var corrupt = receipt.deepCopy();
            switch (invalid) {
                case "owner" -> corrupt.put("account_id", 43);
                case "id" -> corrupt.put("candidate_id", 8);
                case "secret" -> corrupt.put("password_hash", "private-upstream");
                default -> ((ObjectNode) corrupt.get("profile")).putNull("name");
            }
            body = corrupt.toString();
            assertThatThrownBy(() -> client.create("real-session", 42, "stable-intent", input, "creation-trace"))
                    .isInstanceOfSatisfying(AuthException.class, e -> assertThat(e.code()).isEqualTo("PROFILE_CREATE_UNAVAILABLE"));
        }
        status = 409;
        body = "{\"code\":\"PROFILE_CREATE_CONFLICT\",\"detail\":\"private-upstream\"}";
        assertThatThrownBy(() -> client.create("real-session", 42, "stable-intent", input, "creation-trace"))
                .isInstanceOfSatisfying(AuthException.class, e -> assertThat(e.status().value()).isEqualTo(409)).hasMessageNotContaining("private-upstream");
        status = 401;
        body = "{\"code\":\"INTERNAL_UNAUTHORIZED\",\"detail\":\"private-upstream\"}";
        assertThatThrownBy(() -> client.create("real-session", 42, "stable-intent", input, "creation-trace"))
                .isInstanceOfSatisfying(AuthException.class, e -> assertThat(e.status().value()).isEqualTo(503));
    }

    @Test void deletionValidatesResourceOwnerAndCleanupStateWithoutExposingObjectLocators() {
        var receipt = mapper.createObjectNode().put("account_id", 42).put("candidate_id", 7).put("deleted", true)
                .put("cleanup_status", "pending").put("rag_cleanup", "database_cascade");
        body = receipt.toString();
        var client = client(2000);
        var result = client.delete("real-session", 42, 7, "deletion-trace");
        assertThat(result.get("cleanup_status").asText()).isEqualTo("pending");
        assertThat(result.has("account_id")).isFalse();
        assertThat(requests.getFirst()).startsWith("POST /internal/v1/profiles/delete ").doesNotContain("account_id");
        for (String invalid : List.of("owner", "id", "secret", "state", "deleted", "rag")) {
            var value = receipt.deepCopy();
            switch (invalid) {
                case "owner" -> value.put("account_id", 43);
                case "id" -> value.put("candidate_id", 8);
                case "secret" -> value.put("storage_keys", "private-upstream");
                case "state" -> value.put("cleanup_status", "unknown");
                case "deleted" -> value.put("deleted", "true");
                default -> value.put("rag_cleanup", "unknown");
            }
            body = value.toString();
            assertThatThrownBy(() -> client.delete("real-session", 42, 7, "deletion-trace"))
                    .isInstanceOfSatisfying(AuthException.class, e -> assertThat(e.code()).isEqualTo("PROFILE_DELETE_UNAVAILABLE"));
        }
        status = 409;
        body = "{\"code\":\"PROFILE_DELETE_BUSY\",\"detail\":\"private-upstream\"}";
        assertThatThrownBy(() -> client.delete("real-session", 42, 7, "deletion-trace"))
                .isInstanceOfSatisfying(AuthException.class, e -> assertThat(e.status().value()).isEqualTo(409)).hasMessageNotContaining("private-upstream");
        status = 404;
        body = "{\"code\":\"PROFILE_NOT_FOUND\",\"detail\":\"private-upstream\"}";
        assertThatThrownBy(() -> client.delete("real-session", 42, 7, "deletion-trace"))
                .isInstanceOfSatisfying(AuthException.class, e -> assertThat(e.status().value()).isEqualTo(404));
        status = 401;
        body = "{\"code\":\"INTERNAL_UNAUTHORIZED\",\"detail\":\"private-upstream\"}";
        assertThatThrownBy(() -> client.delete("real-session", 42, 7, "deletion-trace"))
                .isInstanceOfSatisfying(AuthException.class, e -> assertThat(e.status().value()).isEqualTo(503));
    }

    @ParameterizedTest @ValueSource(strings = {"owner", "secret", "missing", "id", "duplicate", "name", "number", "skills", "list", "weights", "salary", "root"})
    void rejectsCorruptOrCrossTenantProjection(String invalid) {
        var value = fixture(false);
        var row = (ObjectNode) value.get("profiles").get(0);
        switch (invalid) {
            case "owner" -> value.put("account_id", 43);
            case "secret" -> row.put("password_hash", "private-upstream");
            case "missing" -> row.remove("education");
            case "id" -> row.put("id", false);
            case "duplicate" -> value.withArray("profiles").add(row.deepCopy());
            case "name" -> row.putNull("name");
            case "number" -> row.put("experience_years", "NaN");
            case "skills" -> row.putObject("skills").put("Python", 1);
            case "list" -> row.put("preferred_cities", "杭州");
            case "weights" -> row.putObject("preference_weights").put("city", true);
            case "salary" -> row.put("salary_floor_k", "10");
            case "root" -> value.put("secret", "private-upstream");
        }
        body = value.toString();
        unavailable(client(2000));
    }

    @Test void detailMustMatchRequestedIdAndEmptyListIsARealSuccessfulResponse() {
        body = fixture(true).toString();
        assertThatThrownBy(() -> client(2000).query("real-session", 42, 8L, "profile-query-trace"))
                .isInstanceOfSatisfying(AuthException.class, e -> assertThat(e.status().value()).isEqualTo(503));
        body = "{\"account_id\":42,\"profiles\":[]}";
        assertThat(client(2000).query("real-session", 42, null, "profile-query-trace").get("profiles").isEmpty()).isTrue();
    }

    @Test void malformedDuplicateTrailingJsonAndWrongContentTypeFailClosed() {
        var client = client(2000);
        for (String value : List.of("null", "[]", "{", fixture(false) + " {}",
                fixture(false).toString().replace("\"account_id\":42", "\"account_id\":42,\"account_id\":42"))) {
            body = value;
            unavailable(client);
        }
        contentType = "text/html";
        body = fixture(false).toString();
        unavailable(client);
    }

    @ParameterizedTest @ValueSource(ints = {301, 302, 401, 404, 429, 500, 503})
    void unknownErrorsAndRedirectsAreNotAUserErrorOrEmptySuccess(int failure) {
        status = failure;
        body = "private-upstream";
        unavailable(client(2000));
        assertThat(requests).hasSize(1);
    }

    @Test void onlyKnownCallbackErrorsArePreservedWithoutReflectingDetails() {
        for (int failure : List.of(401, 403, 404, 413)) {
            status = failure;
            String code = switch (failure) { case 401 -> "SESSION_EXPIRED"; case 403 -> "ACCOUNT_DISABLED"; case 404 -> "PROFILE_NOT_FOUND"; default -> "PROFILE_QUERY_TOO_LARGE"; };
            body = mapper.createObjectNode().put("code", code).put("detail", "private-upstream").toString();
            assertThatThrownBy(() -> client(2000).query("real-session", 42, 7L, "profile-query-trace"))
                    .isInstanceOfSatisfying(AuthException.class, e -> assertThat(e.status().value()).isEqualTo(failure))
                    .hasMessageNotContaining("private-upstream");
        }
        status = 401;
        body = "{\"code\":\"INTERNAL_UNAUTHORIZED\",\"detail\":\"wrong service token\"}";
        unavailable(client(2000));
    }

    @Test void limitsBodyAndWholeBodyDeadlineAndRecoversSlots() {
        body = " ".repeat(ProfileQueryClient.MAX_BYTES + 1);
        unavailable(client(2000));
        body = fixture(false).toString();
        delay = 500;
        var client = client(100);
        unavailable(client);
        stall = true;
        unavailable(client);
        delay = 0;
        stall = false;
        assertThat(client.query("real-session", 42, null, "profile-query-trace").has("profiles")).isTrue();
    }

    @Test void rejectsNinthConcurrentCallbackWithoutMakingAnotherHttpRequest() throws Exception {
        releaseBodies = new CountDownLatch(1);
        var client = client(2000);
        // Do not depend on common-pool parallelism or the runner's CPU count.
        try (var callers = Executors.newFixedThreadPool(8)) {
            var pending = java.util.stream.IntStream.range(0, 8).mapToObj(i ->
                    CompletableFuture.supplyAsync(() -> client.query("real-session", 42, null, "profile-query-trace"), callers)).toList();
            try {
                long deadline = System.nanoTime() + Duration.ofSeconds(2).toNanos();
                while (requests.size() < 8 && System.nanoTime() < deadline) Thread.sleep(5);
                assertThat(requests).hasSize(8);
                unavailable(client);
                assertThat(requests).hasSize(8);
            } finally {
                releaseBodies.countDown();
                for (var future : pending) future.join();
            }
        }
    }

    @Test void validatesPrivateOriginTokenAndTimeoutsWithoutReflectingSettings() {
        for (String url : List.of("http://user:private@web:8000", "http://web:8000/api", "http://web?private=x", "http://8.8.8.8", "https://public.example.com")) {
            assertThatThrownBy(() -> new ProfileQueryClient(mapper, url, token, 1000, 2000, "production", "https://public.example.com"))
                    .isInstanceOf(IllegalArgumentException.class).hasMessageNotContaining("private@");
        }
        assertThatCode(() -> new ProfileQueryClient(mapper, "http://web:8000", token, 1000, 2000, "production", "https://public.example.com")).doesNotThrowAnyException();
        assertThatThrownBy(() -> new ProfileQueryClient(mapper, origin, "short", 1000, 2000, "production", "https://public.example.com")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> client(2001)).isInstanceOf(IllegalArgumentException.class);
    }
}
