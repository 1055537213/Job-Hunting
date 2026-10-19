package com.jobhunting.platform.profiles;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
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
        delay = 700;
        var client = client(2000);
        var pending = java.util.stream.IntStream.range(0, 8).mapToObj(i ->
                CompletableFuture.supplyAsync(() -> client.query("real-session", 42, null, "profile-query-trace"))).toList();
        try {
            long deadline = System.nanoTime() + Duration.ofSeconds(2).toNanos();
            while (requests.size() < 8 && System.nanoTime() < deadline) Thread.sleep(5);
            assertThat(requests).hasSize(8);
            unavailable(client);
            assertThat(requests).hasSize(8);
        } finally { for (var future : pending) future.join(); }
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
