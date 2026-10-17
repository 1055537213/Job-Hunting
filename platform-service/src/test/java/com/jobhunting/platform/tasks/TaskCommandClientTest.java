package com.jobhunting.platform.tasks;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import com.jobhunting.platform.auth.AccountAdministrationService;
import com.jobhunting.platform.auth.AuthException;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.http.HttpStatus;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

class TaskCommandClientTest {
    static final ObjectMapper MAPPER = new ObjectMapper();
    static final String TOKEN = "test-internal-service-secret-123456";
    static final String SESSION = "actual-session-secret";
    final List<Captured> requests = new CopyOnWriteArrayList<>();
    final AccountAdministrationService accounts = mock(AccountAdministrationService.class);
    ServerSocket server;
    ExecutorService executor;
    String baseUrl;
    volatile int responseStatus;
    volatile String responseBody;
    volatile String contentType;
    volatile boolean dropResponse;
    volatile int delayMs;
    TaskCommandClient client;
    TaskCommandService service;

    @BeforeEach void setup() throws Exception {
        server = new ServerSocket();
        server.bind(new InetSocketAddress("127.0.0.1", 0));
        executor = Executors.newCachedThreadPool();
        executor.submit(() -> {
            while (!server.isClosed()) {
                try {
                    Socket socket = server.accept();
                    executor.submit(() -> handle(socket));
                } catch (IOException error) {
                    if (!server.isClosed()) throw new IllegalStateException("HTTP stub accept failed", error);
                }
            }
        });
        baseUrl = "http://127.0.0.1:" + server.getLocalPort();
        client = client(1000);
        service = new TaskCommandService(accounts, client);
        when(accounts.authorizeAdministrator(SESSION)).thenReturn(actor(7));
        responseStatus = 200;
        contentType = "application/json; charset=utf-8";
        responseBody = response(false).toString();
    }

    @AfterEach void close() throws IOException {
        if (server != null) server.close();
        if (executor != null) executor.shutdownNow();
    }

    @Test void probeSendsExactContractAndOnlyActualIdentity() {
        JsonNode result = service.submit(SESSION, "probe", null, "probe-key-123", "request-id-123");
        assertThat(result.get("task").size()).isEqualTo(12);
        assertThat(requests).hasSize(1);
        var captured = requests.getFirst();
        assertThat(captured.method()).isEqualTo("POST");
        assertThat(captured.path()).isEqualTo("/internal/v1/task-commands");
        assertThat(captured.token()).isEqualTo(TOKEN);
        assertThat(captured.requestId()).isEqualTo("request-id-123");
        assertThat(captured.traceId()).isEqualTo("request-id-123");
        assertThat(captured.body().size()).isEqualTo(5);
        assertThat(captured.body().get("session_token").asText()).isEqualTo(SESSION);
        assertThat(captured.body().get("operation").asText()).isEqualTo("probe");
        assertThat(captured.body().get("task_key").isNull()).isTrue();
        assertThat(captured.body().get("request_id").asText()).isEqualTo("request-id-123");
        assertThat(captured.body().get("command_id").asText()).matches("[a-f0-9]{64}");
        assertThat(captured.body().toString()).doesNotContain("probe-key-123", "actor_account_id");
        verify(accounts).authorizeAdministrator(SESSION);
    }

    @Test void retryKeepsAdminProjectionAndRequestedTask() {
        responseBody = response(true).toString();
        JsonNode result = service.submit(SESSION, "retry", "task-123", "retry-key-123", "request-id-123");
        assertThat(result.get("task").size()).isEqualTo(14);
        assertThat(result.get("task").get("account_id").asLong()).isEqualTo(20);
        assertThat(result.get("task").get("candidate_id").isNull()).isTrue();
        assertThat(requests.getFirst().body().get("task_key").asText()).isEqualTo("task-123");
        assertThat(requests.getFirst().body().get("operation").asText()).isEqualTo("retry");
    }

    @Test void stableIdExcludesIntentAndSessionButSeparatesActors() {
        service.submit(SESSION, "probe", null, "shared-key-123", "request-id-123");
        String commandId = requests.getFirst().body().get("command_id").asText();
        service.submit(SESSION, "probe", null, "shared-key-123", "request-id-456");
        assertThat(requests.get(1).body().get("command_id").asText()).isEqualTo(commandId);
        responseStatus = 409;
        responseBody = "{\"detail\":\"Key already used for different intent.\",\"code\":\"TASK_COMMAND_CONFLICT\"}";
        assertThatThrownBy(() -> service.submit(SESSION, "retry", "task-123", "shared-key-123", "request-id-789"))
                .isInstanceOfSatisfying(AuthException.class, error -> {
                    assertThat(error.status()).isEqualTo(HttpStatus.CONFLICT);
                    assertThat(error.code()).isEqualTo("TASK_COMMAND_CONFLICT");
                });
        assertThat(requests.get(2).body().get("command_id").asText()).isEqualTo(commandId);
        assertThatThrownBy(() -> service.submit(SESSION, "retry", "different-task", "shared-key-123", "request-id-789"))
                .isInstanceOf(AuthException.class);
        assertThat(requests.get(3).body().get("command_id").asText()).isEqualTo(commandId);
        responseStatus = 200;
        responseBody = response(false).toString();
        when(accounts.authorizeAdministrator("rotated-session")).thenReturn(actor(7));
        service.submit("rotated-session", "probe", null, "shared-key-123", "request-id-123");
        assertThat(requests.get(4).body().get("command_id").asText()).isEqualTo(commandId);
        when(accounts.authorizeAdministrator(SESSION)).thenReturn(actor(8));
        service.submit(SESSION, "probe", null, "shared-key-123", "request-id-123");
        assertThat(requests.get(5).body().get("command_id").asText()).isNotEqualTo(commandId);
    }

    @Test void absentKeysGenerateDistinctCommandsAndValidBoundaryKeysWork() {
        service.submit(SESSION, "probe", null, null, "request-id-123");
        service.submit(SESSION, "probe", null, null, "request-id-123");
        assertThat(requests.get(0).body().get("command_id")).isNotEqualTo(requests.get(1).body().get("command_id"));
        service.submit(SESSION, "probe", null, "A._-1234", "request-id-123");
        service.submit(SESSION, "probe", null, "a".repeat(128), "request-id-123");
        assertThat(requests).hasSize(4);
    }

    @Test void invalidKeysOperationsAndTaskKeysNeverCallPython() {
        for (String key : List.of("", "short", "a".repeat(129), "spaces here", "nonascii-\u00e9", "newline\nkey")) {
            assertThatThrownBy(() -> service.submit(SESSION, "probe", null, key, "request-id-123"))
                    .isInstanceOfSatisfying(AuthException.class, e -> assertThat(e.status().value()).isEqualTo(422));
        }
        for (String operation : List.of("delete", "PROBE", "", "retry")) {
            assertThatThrownBy(() -> service.submit(SESSION, operation, null, null, "request-id-123")).isInstanceOf(AuthException.class);
        }
        assertThatThrownBy(() -> service.submit(SESSION, "probe", "task-123", null, "request-id-123")).isInstanceOf(AuthException.class);
        assertThatThrownBy(() -> service.submit(SESSION, "retry", " ", null, "request-id-123")).isInstanceOf(AuthException.class);
        assertThatThrownBy(() -> service.submit(SESSION, "retry", "t".repeat(65), null, "request-id-123")).isInstanceOf(AuthException.class);
        assertThat(requests).isEmpty();
    }

    @ParameterizedTest @ValueSource(ints = {400, 401, 403, 404, 409, 422, 503})
    void preservesBusinessStatusWithSafeLocalCodeAndMessage(int status) {
        responseStatus = status;
        responseBody = "{\"detail\":\"Task operation cannot proceed.\",\"code\":\"TASK_REJECTED\"}";
        assertThatThrownBy(this::submit).isInstanceOfSatisfying(AuthException.class, error -> {
            assertThat(error.status().value()).isEqualTo(status);
            assertThat(error.code()).isNotEqualTo("TASK_REJECTED");
            assertThat(error.getMessage()).doesNotContain("Task operation cannot proceed.");
        });
        assertThat(requests).hasSize(1);
    }

    @Test void rejectsUnexpectedOrIncorrectTaskFieldsAndTypes() {
        for (String field : List.of("payload", "session_token", "command_id", "idempotency_key", "account_id")) {
            var body = response(false);
            ((ObjectNode) body.get("task")).put(field, "secret");
            responseBody = body.toString();
            assertUnavailable(this::submit);
        }
        for (String field : List.of("task_key", "task_type", "progress", "result", "created_at", "error_summary")) {
            var body = response(false);
            ((ObjectNode) body.get("task")).remove(field);
            responseBody = body.toString();
            assertUnavailable(this::submit);
        }
        for (String field : List.of("task_key", "status", "progress", "attempt", "max_attempts", "result", "started_at")) {
            var body = response(false);
            ((ObjectNode) body.get("task")).put(field, false);
            responseBody = body.toString();
            assertUnavailable(this::submit);
        }
        var body = response(false);
        ((ObjectNode) body.get("task")).put("progress", 101);
        responseBody = body.toString();
        assertUnavailable(this::submit);
        body = response(true);
        ((ObjectNode) body.get("task")).put("account_id", "20");
        responseBody = body.toString();
        assertUnavailable(() -> service.submit(SESSION, "retry", "task-123", "retry-key-123", "request-id-123"));
        responseBody = response(true).toString();
        assertUnavailable(() -> service.submit(SESSION, "retry", "different-task", "retry-key-123", "request-id-123"));
        assertUnavailable(this::submit);
    }

    @Test void malformedAndLeakingErrorsFailClosedWithoutEchoingUpstream() {
        responseStatus = 409;
        for (String body : List.of("not json", "null", "[]", "{}", "{\"detail\":[],\"code\":\"ERROR\"}",
                "{\"detail\":\"Bad\",\"code\":\"ERROR\",\"debug\":\"secret\"}",
                "{\"detail\":\"Bad\",\"code\":[]} ")) {
            responseBody = body;
            assertUnavailable(this::submit);
        }
        for (String detail : List.of(SESSION, TOKEN, "database password=secret", "arbitrary upstream text")) {
            responseBody = MAPPER.createObjectNode().put("detail", detail).put("code", "ERROR").toString();
            assertThatThrownBy(this::submit).isInstanceOfSatisfying(AuthException.class, error -> {
                assertThat(error.status().value()).isEqualTo(409);
                assertThat(error.code()).isEqualTo("TASK_COMMAND_CONFLICT");
                assertThat(error.getMessage()).doesNotContain(detail, SESSION, TOKEN);
            });
        }
        responseStatus = 200;
        for (String body : List.of("not json", "null", "[]", "{}", response(false) + " {}",
                "{\"task\":{},\"task\":" + response(false).get("task") + "}")) {
            responseBody = body;
            assertUnavailable(this::submit);
        }
        responseBody = " ".repeat(1024 * 1024 + 1);
        assertUnavailable(this::submit);
        contentType = "text/html";
        responseBody = response(false).toString();
        assertUnavailable(this::submit);
    }

    @Test void allowsOnlyKnownChineseBusinessMessages() {
        responseStatus = 409;
        for (String message : List.of("幂等键已用于不同的任务操作。", "只有失败状态的后台任务可以人工重试。",
                "已停用账号的任务不能重试。", "原投递请求已失效，请刷新后重新操作。")) {
            responseBody = MAPPER.createObjectNode().put("detail", message).put("code", "HTTP_409").toString();
            assertThatThrownBy(this::submit).isInstanceOfSatisfying(AuthException.class, error -> {
                assertThat(error.status().value()).isEqualTo(409);
                assertThat(error.getMessage()).isEqualTo(message);
            });
        }
        responseStatus = 503;
        responseBody = "{\"detail\":\"后台任务投递尚未确认，请稍后使用相同请求重试。\",\"code\":\"HTTP_503\"}";
        assertUnavailable(this::submit);
    }

    @ParameterizedTest @ValueSource(ints = {201, 202, 204, 301, 302, 307, 308, 429, 500, 502, 504})
    void unexpectedStatusAndRedirectNeverBecomeSuccessOrGetFollowed(int status) {
        responseStatus = status;
        assertUnavailable(this::submit);
        assertThat(requests).hasSize(1);
    }

    @Test void lostResponseAndReadTimeoutDoNotReplay() {
        dropResponse = true;
        assertUnavailable(this::submit);
        assertThat(requests).hasSize(1);
        dropResponse = false;
        delayMs = 500;
        client = client(50);
        assertThat(Duration.ofNanos(timedSubmit())).isLessThan(Duration.ofSeconds(2));
        assertThat(requests).hasSize(2);
    }

    @Test void refusedConnectionReturnsUnavailable() throws IOException {
        server.close();
        assertUnavailable(this::submit);
        assertThat(requests).isEmpty();
    }

    @Test void validatesOriginsTimeoutsAndProductionConfigurationWithoutLeakingSecrets() {
        for (String origin : List.of("file:///etc/passwd", "http://user:password@web:8000", "http://web:8000/path",
                "http://web:8000?token=secret", "http://web:8000#secret", "http://web:0", "http://web:70000")) {
            assertThatThrownBy(() -> new TaskCommandClient(MAPPER, origin, TOKEN, 1000, 5000, "test", "https://public.example.com"))
                    .isInstanceOf(IllegalArgumentException.class).hasMessageNotContaining(origin);
        }
        for (String origin : List.of("http://public.example.com", "https://8.8.8.8", "http://public.internal")) {
            assertThatThrownBy(() -> new TaskCommandClient(MAPPER, origin, TOKEN, 1000, 5000, " PRODUCTION ", "https://public.internal"))
                    .isInstanceOf(IllegalArgumentException.class);
        }
        for (String origin : List.of("http://web:8000", "https://tasks.svc.cluster.local", "http://10.1.2.3:8000", "http://[fd00::1]:8000")) {
            assertThatCode(() -> new TaskCommandClient(MAPPER, origin, TOKEN, 1000, 5000, "production", "https://public.example.com"))
                    .doesNotThrowAnyException();
        }
        for (int timeout : List.of(0, -1, 30001)) {
            assertThatThrownBy(() -> new TaskCommandClient(MAPPER, baseUrl, TOKEN, 1000, timeout, "test", "https://public.example.com"))
                    .isInstanceOf(IllegalArgumentException.class);
        }
        assertThatThrownBy(() -> new TaskCommandClient(MAPPER, baseUrl, TOKEN, 10001, 5000, "test", "https://public.example.com"))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new TaskCommandClient(MAPPER, "http://web:8000", "short", 1000, 5000, "production", "https://public.example.com"))
                .isInstanceOf(IllegalArgumentException.class);
        client = new TaskCommandClient(MAPPER, baseUrl, "", 1000, 5000, "test", "https://public.example.com");
        assertUnavailable(this::submit);
        assertThat(requests).isEmpty();
    }

    private TaskCommandClient client(int timeout) {
        return new TaskCommandClient(MAPPER, baseUrl, TOKEN, 1000, timeout, "test", "https://public.example.com");
    }

    private void submit() { client.submit("a".repeat(64), "probe", null, SESSION, "request-id-123"); }

    private long timedSubmit() {
        long start = System.nanoTime();
        assertUnavailable(this::submit);
        return System.nanoTime() - start;
    }

    private static void assertUnavailable(org.assertj.core.api.ThrowableAssert.ThrowingCallable call) {
        assertThatThrownBy(call).isInstanceOfSatisfying(AuthException.class, error -> {
            assertThat(error.status()).isEqualTo(HttpStatus.SERVICE_UNAVAILABLE);
            assertThat(error.code()).isEqualTo("TASK_COMMAND_UNAVAILABLE");
            assertThat(error.getMessage()).doesNotContain(SESSION, TOKEN, "password", "secret");
            assertThat(error.getCause()).isNull();
        });
    }

    static ObjectNode response(boolean admin) {
        var body = MAPPER.createObjectNode();
        var task = body.putObject("task");
        task.put("task_key", "task-123");
        task.put("task_type", "system_probe");
        task.put("status", "queued");
        task.put("progress", 0);
        task.put("attempt", 0);
        task.put("max_attempts", 1);
        task.putObject("result");
        task.putNull("error_summary");
        task.put("created_at", "2026-10-17T00:00:00+00:00");
        task.putNull("started_at");
        task.putNull("finished_at");
        task.put("updated_at", "2026-10-17T00:00:00+00:00");
        if (admin) { task.put("account_id", 20); task.putNull("candidate_id"); }
        return body;
    }

    static AccountAdministrationService.AccountView actor(long id) {
        return new AccountAdministrationService.AccountView(id, "admin@example.com", null, "admin", "active", false,
                null, null, "2026-10-17T00:00:00Z", "2026-10-17T00:00:00Z");
    }

    private void handle(Socket socket) {
        try (socket) {
            socket.setSoTimeout(2000);
            var input = socket.getInputStream();
            var head = new ByteArrayOutputStream();
            while (head.size() < 16384) {
                int next = input.read();
                if (next < 0) throw new IOException("Incomplete HTTP request");
                head.write(next);
                if (next == '\n' && head.toString(StandardCharsets.US_ASCII).endsWith("\r\n\r\n")) break;
            }
            String[] lines = head.toString(StandardCharsets.US_ASCII).split("\r\n");
            String[] request = lines[0].split(" ", 3);
            var headers = new org.springframework.http.HttpHeaders();
            for (int index = 1; index < lines.length; index++) {
                int colon = lines[index].indexOf(':');
                headers.add(lines[index].substring(0, colon), lines[index].substring(colon + 1).strip());
            }
            requests.add(new Captured(request[0], request[1], headers.getFirst("X-Internal-Service-Token"),
                    headers.getFirst("X-Request-ID"), headers.getFirst("X-Trace-Id"),
                    MAPPER.readTree(input.readNBytes(Math.toIntExact(headers.getContentLength())))));
            if (dropResponse) return;
            if (delayMs > 0) {
                try { Thread.sleep(delayMs); } catch (InterruptedException error) { Thread.currentThread().interrupt(); return; }
            }
            byte[] response = responseBody.getBytes(StandardCharsets.UTF_8);
            var output = socket.getOutputStream();
            output.write(("HTTP/1.1 " + responseStatus + " Stub\r\nContent-Type: " + contentType
                    + "\r\nLocation: " + baseUrl + "/redirect-target\r\nContent-Length: "
                    + (responseStatus == 204 ? 0 : response.length) + "\r\nConnection: close\r\n\r\n")
                    .getBytes(StandardCharsets.US_ASCII));
            if (responseStatus != 204) output.write(response);
        } catch (IOException ignored) {
            // Timeout/oversize cases intentionally disconnect while the fixture is still writing.
        }
    }

    private record Captured(String method, String path, String token, String requestId, String traceId, JsonNode body) {
        @Override public String toString() { return "CapturedTaskCommand[redacted]"; }
    }
}
