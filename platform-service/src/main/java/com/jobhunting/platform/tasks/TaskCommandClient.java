package com.jobhunting.platform.tasks;

import java.io.IOException;
import java.net.ProxySelector;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.time.Duration;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Flow;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicBoolean;

import com.jobhunting.platform.auth.AuthException;
import com.jobhunting.platform.observability.TraceHeaders;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.core.StreamReadFeature;

@Component
@ConditionalOnProperty(prefix = "platform.auth", name = "enabled", havingValue = "true")
public class TaskCommandClient {
    private static final int MAX_RESPONSE_BYTES = 1024 * 1024;
    private static final Set<Integer> BUSINESS_STATUSES = Set.of(400, 401, 403, 404, 409, 422, 503);
    private static final Set<String> TASK_FIELDS = Set.of("task_key", "task_type", "status", "progress", "attempt",
            "max_attempts", "result", "error_summary", "created_at", "started_at", "finished_at", "updated_at");
    private static final Set<String> STATUSES = Set.of("queued", "running", "succeeded", "failed", "cancelled");
    private final URI endpoint;
    private final String internalToken;
    private final int readTimeoutMs;
    private final ObjectMapper mapper;
    private final HttpClient http;

    public TaskCommandClient(ObjectMapper mapper,
            @Value("${platform.tasks.python-base-url:http://web:8000}") String baseUrl,
            @Value("${platform.internal-token:}") String internalToken,
            @Value("${platform.tasks.connect-timeout-ms:1000}") int connectTimeoutMs,
            @Value("${platform.tasks.read-timeout-ms:5000}") int readTimeoutMs,
            @Value("${platform.web-security.environment:development}") String environment,
            @Value("${platform.web-security.public-base-url:http://localhost:8000}") String publicBaseUrl) {
        this.endpoint = endpoint(baseUrl, environment, publicBaseUrl);
        if (connectTimeoutMs < 1 || connectTimeoutMs > 10000 || readTimeoutMs < 1 || readTimeoutMs > 30000) {
            throw new IllegalArgumentException("Invalid task command timeouts");
        }
        if (internalToken.chars().anyMatch(c -> c < 33 || c > 126)
                || ("production".equalsIgnoreCase(environment.strip()) && internalToken.length() < 32)) {
            throw new IllegalArgumentException("Invalid task command service token");
        }
        this.mapper = mapper.rebuild().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
                .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS).build();
        this.internalToken = internalToken;
        this.readTimeoutMs = readTimeoutMs;
        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofMillis(connectTimeoutMs))
                .followRedirects(HttpClient.Redirect.NEVER).proxy(ProxySelector.of(null))
                .version(HttpClient.Version.HTTP_1_1).build();
    }

    public JsonNode submit(String commandId, String operation, String taskKey, String session, String requestId) {
        if (internalToken.isBlank()) throw unavailable();
        var body = mapper.createObjectNode();
        body.put("command_id", commandId);
        body.put("operation", operation);
        if (taskKey == null) body.putNull("task_key"); else body.put("task_key", taskKey);
        body.put("session_token", session);
        body.put("request_id", requestId);
        CompletableFuture<HttpResponse<byte[]>> pending = null;
        try {
            byte[] payload = mapper.writeValueAsBytes(body);
            var request = TraceHeaders.apply(HttpRequest.newBuilder(endpoint), requestId).timeout(Duration.ofMillis(readTimeoutMs))
                    .header("Content-Type", "application/json").header("Accept", "application/json")
                    .header("X-Internal-Service-Token", internalToken).POST(singleUseBody(payload)).build();
            pending = http.sendAsync(request, ignored -> new BoundedBody());
            // The deadline covers the complete response, including a slow/dripping body.
            var response = pending.get(readTimeoutMs, TimeUnit.MILLISECONDS);
            int status = response.statusCode();
            if (status != 200 && !BUSINESS_STATUSES.contains(status)) throw unavailable();
            String contentType = response.headers().firstValue("Content-Type").orElse(null);
            if (contentType == null || !contentType.split(";", 2)[0].strip().equalsIgnoreCase("application/json")) {
                throw unavailable();
            }
            JsonNode result = mapper.readTree(response.body());
            if (status != 200) throw businessError(status, result);
            validateTask(result, operation, taskKey);
            return result;
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw unavailable();
        } catch (ExecutionException | TimeoutException | tools.jackson.core.JacksonException error) {
            // Never attach upstream bodies, request objects, URLs or transport exception messages.
            throw unavailable();
        } finally {
            if (pending != null && !pending.isDone()) pending.cancel(true);
        }
    }

    private static HttpRequest.BodyPublisher singleUseBody(byte[] bytes) {
        var body = HttpRequest.BodyPublishers.ofByteArray(bytes);
        var subscribed = new AtomicBoolean();
        return new HttpRequest.BodyPublisher() {
            @Override public long contentLength() { return bytes.length; }
            @Override public void subscribe(Flow.Subscriber<? super ByteBuffer> subscriber) {
                if (subscribed.compareAndSet(false, true)) {
                    body.subscribe(subscriber);
                } else {
                    subscriber.onSubscribe(new Flow.Subscription() {
                        @Override public void request(long count) { }
                        @Override public void cancel() { }
                    });
                    subscriber.onError(new IOException("Task command replay is disabled"));
                }
            }
        };
    }

    private static final class BoundedBody implements HttpResponse.BodySubscriber<byte[]> {
        private final HttpResponse.BodySubscriber<byte[]> delegate = HttpResponse.BodySubscribers.ofByteArray();
        private Flow.Subscription subscription;
        private long received;

        @Override public CompletionStage<byte[]> getBody() { return delegate.getBody(); }
        @Override public void onSubscribe(Flow.Subscription subscription) {
            this.subscription = subscription;
            delegate.onSubscribe(subscription);
        }
        @Override public void onNext(List<ByteBuffer> buffers) {
            for (ByteBuffer buffer : buffers) received += buffer.remaining();
            if (received > MAX_RESPONSE_BYTES) {
                subscription.cancel();
                delegate.onError(new IOException("Task command response is too large"));
            } else {
                delegate.onNext(buffers);
            }
        }
        @Override public void onError(Throwable error) { delegate.onError(error); }
        @Override public void onComplete() { delegate.onComplete(); }
    }

    private AuthException businessError(int status, JsonNode body) {
        if (!fields(body, Set.of("detail", "code")) || !text(body.get("code"), 80)
                || !body.get("code").asText().matches("[A-Z][A-Z0-9_]{0,79}")
                || !text(body.get("detail"), 512)) throw unavailable();
        String code = switch (status) {
            case 400, 422 -> "INVALID_REQUEST";
            case 401 -> "SESSION_EXPIRED";
            case 403 -> "ADMIN_REQUIRED";
            case 404 -> "TASK_NOT_FOUND";
            case 409 -> "TASK_COMMAND_CONFLICT";
            default -> "TASK_COMMAND_UNAVAILABLE";
        };
        String fallback = switch (status) {
            case 400, 422 -> "无效的任务操作。";
            case 401 -> "登录状态已过期，请重新登录。";
            case 403 -> "需要有效的管理员账号。";
            case 404 -> "后台任务不存在。";
            case 409 -> "任务操作与已有请求或任务状态冲突，请刷新后重试。";
            default -> unavailable().getMessage();
        };
        // Only literal business messages are safe to expose; unknown detail/code values are never reflected.
        Set<String> messages = switch (status) {
            case 400, 422 -> Set.of("无效的任务操作。");
            case 401 -> Set.of("登录状态已过期，请重新登录。", "内部服务认证失败。");
            case 403 -> Set.of("需要管理员权限。", "需要有效的管理员账号。", "账号已被禁用。");
            case 404 -> Set.of("后台任务不存在。");
            case 409 -> Set.of("幂等键已用于不同的任务操作。", "只有失败状态的后台任务可以人工重试。", "已停用账号的任务不能重试。",
                    "原投递请求已失效，请刷新后重新操作。");
            default -> Set.of("后台任务投递尚未确认，请稍后使用相同请求重试。");
        };
        String detail = body.get("detail").asText();
        return new AuthException(code, messages.contains(detail) ? detail : fallback, HttpStatus.valueOf(status));
    }

    private static void validateTask(JsonNode body, String operation, String taskKey) {
        if (!fields(body, Set.of("task"))) throw unavailable();
        JsonNode task = body.get("task");
        var permitted = new HashSet<>(TASK_FIELDS);
        if ("retry".equals(operation)) permitted.addAll(Set.of("account_id", "candidate_id"));
        if (!fields(task, permitted) || !text(task.get("task_key"), 64) || !text(task.get("task_type"), 80)
                || !text(task.get("status"), 20) || !STATUSES.contains(task.get("status").asText())
                || !integer(task.get("progress"), 0, 100) || !integer(task.get("attempt"), 0, Integer.MAX_VALUE)
                || !integer(task.get("max_attempts"), 1, Integer.MAX_VALUE) || !task.get("result").isObject()
                || !nullableText(task.get("error_summary")) || !text(task.get("created_at"), 64)
                || !text(task.get("updated_at"), 64) || !nullableText(task.get("started_at"))
                || !nullableText(task.get("finished_at"))) throw unavailable();
        if ("retry".equals(operation) && (!task.get("task_key").asText().equals(taskKey)
                || !integer(task.get("account_id"), 1, Long.MAX_VALUE)
                || !(task.get("candidate_id").isNull() || integer(task.get("candidate_id"), 1, Long.MAX_VALUE)))) {
            throw unavailable();
        }
    }

    private static boolean fields(JsonNode node, Set<String> fields) {
        if (node == null || !node.isObject() || node.size() != fields.size()) return false;
        var actual = new HashSet<String>();
        node.propertyNames().forEach(actual::add);
        return actual.equals(fields);
    }

    private static boolean text(JsonNode node, int max) {
        return node != null && node.isString() && !node.asText().isBlank() && node.asText().length() <= max;
    }

    private static boolean nullableText(JsonNode node) {
        return node != null && (node.isNull() || node.isString());
    }

    private static boolean integer(JsonNode node, long min, long max) {
        return node != null && node.isIntegralNumber() && node.canConvertToLong()
                && node.asLong() >= min && node.asLong() <= max;
    }

    private static URI endpoint(String baseUrl, String environment, String publicBaseUrl) {
        try {
            URI base = URI.create(baseUrl);
            String host = base.getHost();
            if (!("http".equals(base.getScheme()) || "https".equals(base.getScheme())) || host == null
                    || base.getRawUserInfo() != null || base.getRawQuery() != null || base.getRawFragment() != null
                    || !(base.getRawPath().isEmpty() || "/".equals(base.getRawPath()))
                    || base.getPort() == 0 || base.getPort() > 65535) throw new IllegalArgumentException();
            if ("production".equalsIgnoreCase(environment.strip())
                    && (!internalHost(host) || host.equalsIgnoreCase(URI.create(publicBaseUrl).getHost()))) {
                throw new IllegalArgumentException();
            }
            return base.resolve("/internal/v1/task-commands");
        } catch (IllegalArgumentException error) {
            throw new IllegalArgumentException("Task command base URL must be a service origin; production requires a private internal host");
        }
    }

    private static boolean internalHost(String host) {
        host = host.toLowerCase(Locale.ROOT);
        if (host.matches("[a-z][a-z0-9-]*") || host.endsWith(".internal") || host.endsWith(".local")
                || host.endsWith(".svc") || host.endsWith(".svc.cluster.local") || host.equals("[::1]")
                || host.matches("\\[f[cd][0-9a-f:]+\\]")) return true;
        if (!host.matches("[0-9]{1,3}(\\.[0-9]{1,3}){3}")) return false;
        String[] octets = host.split("\\.");
        int first = Integer.parseInt(octets[0]);
        int second = Integer.parseInt(octets[1]);
        for (String octet : octets) if (Integer.parseInt(octet) > 255) return false;
        return first == 10 || first == 127 || (first == 192 && second == 168) || (first == 172 && second >= 16 && second <= 31);
    }

    private static AuthException unavailable() {
        return new AuthException("TASK_COMMAND_UNAVAILABLE", "后台任务投递尚未确认，请稍后使用相同请求重试。",
                HttpStatus.SERVICE_UNAVAILABLE);
    }
}
