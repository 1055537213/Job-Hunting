package com.jobhunting.platform.observability;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

import static com.jobhunting.platform.observability.ObservabilityFixtures.*;
import static org.assertj.core.api.Assertions.*;

class PythonObservabilityClientTest {
    ServerSocket server;
    ExecutorService executor;
    String origin;
    final List<String> requests = new CopyOnWriteArrayList<>();
    volatile String body;
    volatile String contentType;
    volatile int responseStatus;
    volatile int delayMs;
    volatile boolean stallBody;

    @BeforeEach void setup() throws IOException {
        body = python(3, 30).toString();
        contentType = "application/json; charset=utf-8";
        responseStatus = 200;
        server = new ServerSocket();
        server.bind(new InetSocketAddress("127.0.0.1", 0));
        origin = "http://127.0.0.1:" + server.getLocalPort();
        executor = Executors.newCachedThreadPool();
        executor.submit(() -> {
            while (!server.isClosed()) {
                try {
                    var socket = server.accept();
                    executor.submit(() -> respond(socket));
                } catch (IOException closed) {
                    if (!server.isClosed()) throw new IllegalStateException("Stub accept failed");
                }
            }
        });
    }

    @AfterEach void close() throws IOException {
        server.close();
        executor.shutdownNow();
    }

    @Test void sendsOnlyPrivateServiceCredentialAndDoesNotCache() {
        var client = client(1000);
        assertThat(client.snapshot().path("status").asText()).isEqualTo("available");
        body = python(0, 0).toString();
        assertThat(client.snapshot().path("api").path("total_requests").asLong()).isZero();
        assertThat(requests).hasSize(2);
        assertThat(requests.getFirst()).startsWith("GET /internal/v1/observability/requests HTTP/1.1")
                .contains("X-Internal-Service-Token: " + TOKEN).doesNotContain(SESSION, "Cookie:", "session_token");
    }

    @Test void emailUnavailableLeavesGoodMetricsAvailableAndSafe() {
        var value = python(3, 30);
        value.set("account_email", ObservabilityProjection.unavailableEmail(MAPPER));
        body = value.toString();
        var result = client(1000).snapshot();
        assertThat(result.path("status").asText()).isEqualTo("available");
        assertThat(result.path("api").path("total_requests").asLong()).isEqualTo(3);
        assertThat(result.path("account_email").path("summary").isNull()).isTrue();
        assertThat(result.path("account_email").path("records").isEmpty()).isTrue();
    }

    @Test void masksLegacyRecipientsAndRegeneratesErrorText() {
        var value = python(3, 30);
        ((ArrayNode) value.get("account_email").get("records")).add(record("python", "verify_email", 1, NOW));
        body = value.toString();
        var result = client(1000).snapshot();
        assertThat(result.path("account_email").path("status").asText()).isEqualTo("available");
        assertThat(result.toString()).doesNotContain("raw-recipient", "private.example", "smtp://", "password", "secret-token", "SECRET_RAW_EXCEPTION");
    }

    @ParameterizedTest @ValueSource(ints = {301, 302, 401, 403, 429, 500, 503})
    void non200NeverReflectsBodiesOrFollowsRedirect(int status) {
        responseStatus = status;
        body = "upstream-secret " + TOKEN;
        var result = client(1000).snapshot();
        assertUnavailable(result);
        assertThat(requests).hasSize(1);
    }

    @Test void malformedJsonDuplicateFieldsTrailingTokensAndUnexpectedFieldsDegrade() {
        var client = client(1000);
        for (String malformed : List.of("{", "null", "[]", "{}", python(3, 30) + " {}",
                python(3, 30).toString().replace("\"status\":\"available\"", "\"status\":\"available\",\"status\":\"available\""))) {
            body = malformed;
            assertUnavailable(client.snapshot());
        }
        var value = python(3, 30);
        ((ObjectNode) value.get("api")).put("secret", TOKEN);
        body = value.toString();
        assertUnavailable(client.snapshot());
        value = python(3, 30);
        ((ObjectNode) value.get("api")).putNull("total_requests");
        body = value.toString();
        assertUnavailable(client.snapshot());
        body = python(3, 30).toString();
        contentType = "text/html";
        assertUnavailable(client.snapshot());
    }

    @Test void invalidSafeErrorFieldsRejectMetricsWhileBadEmailDegradesOnlyEmail() {
        var value = python(3, 30);
        var error = error("unsafe ?id", NOW);
        ((ArrayNode) value.get("api").get("recent_errors")).add(error);
        body = value.toString();
        assertUnavailable(client(1000).snapshot());
        value = python(3, 30);
        ((ObjectNode) value.get("account_email")).put("secret", TOKEN);
        body = value.toString();
        var result = client(1000).snapshot();
        assertThat(result.path("status").asText()).isEqualTo("available");
        assertThat(result.path("account_email").path("status").asText()).isEqualTo("unavailable");
        assertThat(result.toString()).doesNotContain(TOKEN);
    }

    @Test void oversizedBodyIsBounded() {
        body = " ".repeat(1024 * 1024 + 1);
        assertUnavailable(client(1000).snapshot());
    }

    @Test void sharedCredentialCannotBeReflectedThroughAnOtherwiseSafeIdentifier() {
        var value = python(3, 30);
        ((ArrayNode) value.get("api").get("recent_errors")).add(error(TOKEN, NOW));
        body = value.toString();
        assertUnavailable(client(1000).snapshot());
    }

    @Test void deadlineBoundsHeadersAndStalledResponseBody() {
        delayMs = 700;
        assertTimeoutDegrades();
        delayMs = 0;
        stallBody = true;
        assertTimeoutDegrades();
    }

    @Test void productionValidatesPrivateOriginSharedCredentialAndBoundedTimeouts() {
        assertThatCode(() -> new PythonObservabilityClient(MAPPER, "http://web:8000", TOKEN, 1000, 2000, "production", "https://app.example.com"))
                .doesNotThrowAnyException();
        for (String url : List.of("https://public.example.com", "http://8.8.8.8", "http://user:pass@web:8000", "http://web:8000/api", "http://web:8000?secret=x")) {
            assertThatThrownBy(() -> new PythonObservabilityClient(MAPPER, url, TOKEN, 1000, 2000, "production", "https://app.example.com"))
                    .isInstanceOf(IllegalArgumentException.class).hasMessageNotContaining("secret=x").hasMessageNotContaining("user:pass");
        }
        assertThatThrownBy(() -> new PythonObservabilityClient(MAPPER, "http://web:8000", "short", 1000, 2000, "production", "https://app.example.com"))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new PythonObservabilityClient(MAPPER, origin, TOKEN, 1001, 2000, "test", "http://localhost:8000"))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new PythonObservabilityClient(MAPPER, origin, TOKEN, 1000, 2001, "test", "http://localhost:8000"))
                .isInstanceOf(IllegalArgumentException.class);
        var noCredential = new PythonObservabilityClient(MAPPER, origin, "", 1000, 2000, "test", "http://localhost:8000");
        assertUnavailable(noCredential.snapshot());
        assertThat(requests).isEmpty();
    }

    private void assertTimeoutDegrades() {
        var client = client(100);
        long start = System.nanoTime();
        assertUnavailable(client.snapshot());
        assertThat(Duration.ofNanos(System.nanoTime() - start)).isLessThan(Duration.ofMillis(600));
    }

    private PythonObservabilityClient client(int timeout) {
        return new PythonObservabilityClient(MAPPER, origin, TOKEN, 1000, timeout, "test", "http://localhost:8000");
    }

    private void assertUnavailable(ObjectNode result) {
        assertThat(result.path("status").asText()).isEqualTo("unavailable");
        assertThat(result.path("api").isNull()).isTrue();
        assertThat(result.path("internal").isNull()).isTrue();
        assertThat(result.path("observed_at").isNull()).isTrue();
        assertThat(result.toString()).doesNotContain(TOKEN, "upstream-secret", "secret=x");
    }

    private void respond(Socket socket) {
        try (socket) {
            socket.setSoTimeout(2000);
            var request = new ByteArrayOutputStream();
            int next;
            while ((next = socket.getInputStream().read()) != -1) {
                request.write(next);
                byte[] bytes = request.toByteArray();
                int n = bytes.length;
                if (n >= 4 && bytes[n - 4] == '\r' && bytes[n - 3] == '\n' && bytes[n - 2] == '\r' && bytes[n - 1] == '\n') break;
                if (n > 16384) return;
            }
            requests.add(request.toString(StandardCharsets.US_ASCII));
            String capturedBody = body;
            int capturedDelay = delayMs;
            boolean capturedStall = stallBody;
            if (capturedDelay > 0) Thread.sleep(capturedDelay);
            byte[] payload = capturedBody.getBytes(StandardCharsets.UTF_8);
            String headers = "HTTP/1.1 " + responseStatus + " Stub\r\nContent-Type: " + contentType
                    + "\r\nLocation: " + origin + "/redirect-secret\r\nContent-Length: " + payload.length + "\r\nConnection: close\r\n\r\n";
            socket.getOutputStream().write(headers.getBytes(StandardCharsets.US_ASCII));
            socket.getOutputStream().flush();
            if (capturedStall) Thread.sleep(700);
            socket.getOutputStream().write(payload);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        } catch (IOException closed) {
            // Timeout cancellation and body-size rejection close the socket intentionally.
        }
    }
}
