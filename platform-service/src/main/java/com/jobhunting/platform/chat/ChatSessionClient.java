package com.jobhunting.platform.chat;

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
import java.util.concurrent.Flow;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import com.jobhunting.platform.auth.AuthException;
import com.jobhunting.platform.observability.TraceHeaders;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import tools.jackson.core.StreamReadFeature;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

@Component
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class ChatSessionClient {
    static final int MAX_BYTES = 4 * 1024 * 1024;
    private static final Set<String> SESSION_FIELDS = Set.of("id", "session_id", "account_id", "candidate_id", "job_id",
            "title", "status", "created_at", "updated_at", "archived_at");
    private static final Set<String> MESSAGE_FIELDS = Set.of("id", "candidate_id", "session_id", "role", "content", "metadata", "created_at");
    private final ObjectMapper mapper;
    private final HttpClient http;
    private final URI endpoint;
    private final String token;
    private final Semaphore slots = new Semaphore(8);

    public ChatSessionClient(ObjectMapper mapper,
            @Value("${platform.chat.python-base-url:http://web:8000}") String baseUrl,
            @Value("${platform.internal-token:}") String token,
            @Value("${platform.web-security.environment:development}") String environment,
            @Value("${platform.web-security.public-base-url:http://localhost:8000}") String publicUrl) {
        endpoint = endpoint(baseUrl, environment, publicUrl);
        if (token.chars().anyMatch(c -> c < 33 || c > 126)
                || ("production".equalsIgnoreCase(environment.strip()) && token.length() < 32))
            throw new IllegalArgumentException("Invalid chat callback credentials");
        this.token = token;
        this.mapper = mapper.rebuild().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
                .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS).build();
        http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(1)).proxy(ProxySelector.of(null))
                .followRedirects(HttpClient.Redirect.NEVER).version(HttpClient.Version.HTTP_1_1).build();
    }

    public JsonNode execute(String operation, String session, long owner, ObjectNode data, String requestId) {
        var payload = data.deepCopy().put("session_token", session);
        var value = exchange(operation, payload, requestId);
        if (!id(value.get("account_id")) || value.get("account_id").asLong() != owner) throw unavailable();
        var result = (ObjectNode) value.deepCopy();
        result.remove("account_id");
        validate(result, operation, owner, data);
        return result;
    }

    private JsonNode exchange(String operation, JsonNode payload, String requestId) {
        if (token.isBlank() || !slots.tryAcquire()) throw unavailable();
        CompletableFuture<HttpResponse<byte[]>> pending = null;
        try {
            var request = TraceHeaders.apply(HttpRequest.newBuilder(endpoint.resolve(operation)), requestId)
                    .timeout(Duration.ofSeconds(10)).header("Content-Type", "application/json").header("Accept", "application/json")
                    .header("X-Internal-Service-Token", token).POST(singleUse(mapper.writeValueAsBytes(payload))).build();
            pending = http.sendAsync(request, ignored -> new BoundedBody());
            var response = pending.get(10, TimeUnit.SECONDS);
            if (!response.headers().firstValue("Content-Type").orElse("").split(";", 2)[0].strip().equalsIgnoreCase("application/json")) throw unavailable();
            var value = mapper.readTree(response.body());
            if (value == null || !value.isObject()) throw unavailable();
            if (response.statusCode() != 200) {
                if (!fields(value, Set.of("code", "detail")) || !value.path("code").isTextual() || !value.path("detail").isTextual()) throw unavailable();
                throw safeError(response.statusCode(), value.get("code").asText());
            }
            return value;
        } catch (AuthException error) { throw error;
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt(); throw unavailable();
        } catch (Exception error) { throw unavailable();
        } finally {
            if (pending != null && !pending.isDone()) pending.cancel(true);
            slots.release();
        }
    }

    static void validate(JsonNode value, String operation, long owner, JsonNode request) {
        Long candidate = request.path("candidate_id").isNull() || request.get("candidate_id") == null ? null : request.get("candidate_id").asLong();
        String key = request.path("session_id").isTextual() ? request.get("session_id").asText() : null;
        if (Set.of("list", "create", "archive").contains(operation)) {
            String field = "list".equals(operation) ? "sessions" : "session";
            if (!fields(value, Set.of(field))) throw unavailable();
            var rows = "list".equals(operation) ? value.get(field) : new ObjectMapper().createArrayNode().add(value.get(field));
            if (!rows.isArray()) throw unavailable();
            var ids = new HashSet<Long>(); var keys = new HashSet<String>();
            for (var row : rows) {
                if (!fields(row, SESSION_FIELDS) || !id(row.get("id")) || !id(row.get("account_id")) || row.get("account_id").asLong() != owner
                        || !id(row.get("candidate_id")) || (candidate != null && row.get("candidate_id").asLong() != candidate)
                        || !sessionKey(row.get("session_id")) || (key != null && !key.equals(row.get("session_id").asText()))
                        || (!row.get("job_id").isNull() && !id(row.get("job_id")))
                        || !row.get("status").isTextual() || !Set.of("active", "archived").contains(row.get("status").asText())
                        || !row.get("title").isTextual() || !row.get("created_at").isTextual() || !row.get("updated_at").isTextual()
                        || !(row.get("archived_at").isNull() || row.get("archived_at").isTextual())
                        || !ids.add(row.get("id").asLong()) || !keys.add(row.get("session_id").asText())) throw unavailable();
                if (("archive".equals(operation) && !"archived".equals(row.get("status").asText()))
                        || (("create".equals(operation) || ("list".equals(operation) && !request.path("include_archived").asBoolean()))
                            && !"active".equals(row.get("status").asText()))) throw unavailable();
                if ("create".equals(operation) && (row.get("job_id").isNull() != request.path("job_id").isNull()
                        || (!row.get("job_id").isNull() && row.get("job_id").asLong() != request.path("job_id").asLong()))) throw unavailable();
            }
            return;
        }
        boolean history = "history".equals(operation);
        if (!fields(value, history ? Set.of("candidate_id", "session_id", "messages") : Set.of("candidate_id", "session_id", "deleted"))
                || !id(value.get("candidate_id")) || (candidate != null && value.get("candidate_id").asLong() != candidate)
                || !sessionKey(value.get("session_id"))) throw unavailable();
        if (key == null && history) key = "account-" + owner + "-candidate-" + candidate;
        if (!value.get("session_id").asText().equals(key)) throw unavailable();
        if (!history) {
            if (!value.get("deleted").isBoolean() || !value.get("deleted").asBoolean()) throw unavailable();
            return;
        }
        var messages = value.get("messages"); var ids = new HashSet<Long>();
        if (!messages.isArray() || messages.size() > request.path("limit").asInt(100)) throw unavailable();
        for (var row : messages) {
            if (!fields(row, MESSAGE_FIELDS) || !id(row.get("id")) || !ids.add(row.get("id").asLong())
                    || !row.get("candidate_id").equals(value.get("candidate_id")) || !row.get("session_id").equals(value.get("session_id"))
                    || !row.get("role").isTextual() || !Set.of("user", "assistant").contains(row.get("role").asText())
                    || !row.get("content").isTextual() || !row.get("created_at").isTextual()
                    || !row.get("metadata").isObject() || !Set.of("task_trace").containsAll(row.get("metadata").propertyNames())) throw unavailable();
        }
    }

    static boolean id(JsonNode node) { return node != null && node.isIntegralNumber() && node.canConvertToLong() && node.asLong() > 0; }
    static boolean sessionKey(JsonNode node) { return node != null && node.isTextual() && node.asText().matches("[A-Za-z0-9_-]{1,128}"); }
    static boolean fields(JsonNode node, Set<String> fields) { return node != null && node.isObject() && node.propertyNames().equals(fields); }
    static AuthException unavailable() { return new AuthException("CHAT_UNAVAILABLE", "会话操作结果暂未确认，请刷新会话列表核实后重试。", HttpStatus.SERVICE_UNAVAILABLE); }
    static AuthException invalid() { return new AuthException("INVALID_REQUEST", "请求参数无效。", HttpStatus.UNPROCESSABLE_CONTENT); }
    private static AuthException safeError(int status, String code) {
        String detail = switch (status + ":" + code) {
            case "401:SESSION_EXPIRED" -> "登录状态已过期，请重新登录。";
            case "403:CHAT_SCOPE_DENIED" -> "账号不可用或无权访问会话。";
            case "404:CHAT_NOT_FOUND" -> "会话、档案或职位不存在。";
            case "409:CHAT_SESSION_ARCHIVED" -> "该会话已经归档，请新建对话。";
            case "409:CHAT_BUSY" -> "当前仍有请求执行，请稍后重试。";
            case "413:CHAT_TOO_LARGE" -> "会话数据超出同步查询限制。";
            case "422:INVALID_REQUEST" -> "请求参数无效。";
            default -> null;
        };
        return detail == null ? unavailable() : new AuthException(code, detail, HttpStatus.valueOf(status));
    }

    private static HttpRequest.BodyPublisher singleUse(byte[] bytes) {
        var body = HttpRequest.BodyPublishers.ofByteArray(bytes); var used = new AtomicBoolean();
        return new HttpRequest.BodyPublisher() {
            @Override public long contentLength() { return bytes.length; }
            @Override public void subscribe(Flow.Subscriber<? super ByteBuffer> subscriber) {
                if (used.compareAndSet(false, true)) body.subscribe(subscriber);
                else {
                    subscriber.onSubscribe(new Flow.Subscription() { public void request(long n) { } public void cancel() { } });
                    subscriber.onError(new IOException("Chat callback replay disabled"));
                }
            }
        };
    }
    private static final class BoundedBody implements HttpResponse.BodySubscriber<byte[]> {
        private final HttpResponse.BodySubscriber<byte[]> delegate = HttpResponse.BodySubscribers.ofByteArray();
        private Flow.Subscription subscription;
        private long received;
        private boolean failed;
        @Override public CompletionStage<byte[]> getBody() { return delegate.getBody(); }
        @Override public void onSubscribe(Flow.Subscription s) { subscription = s; delegate.onSubscribe(s); }
        @Override public void onNext(List<ByteBuffer> buffers) {
            if (failed) return;
            for (var buffer : buffers) received += buffer.remaining();
            if (received > MAX_BYTES) {
                failed = true; subscription.cancel(); delegate.onError(new IOException("Chat response too large"));
            } else delegate.onNext(buffers);
        }
        @Override public void onError(Throwable error) { if (!failed) delegate.onError(error); }
        @Override public void onComplete() { if (!failed) delegate.onComplete(); }
    }
    private static URI endpoint(String url, String environment, String publicUrl) {
        try {
            var base = URI.create(url); String host = base.getHost();
            if (!("http".equals(base.getScheme()) || "https".equals(base.getScheme())) || host == null
                    || base.getRawUserInfo() != null || base.getRawQuery() != null || base.getRawFragment() != null
                    || !(base.getRawPath().isEmpty() || "/".equals(base.getRawPath())) || base.getPort() == 0 || base.getPort() > 65535) throw new IllegalArgumentException();
            if ("production".equalsIgnoreCase(environment.strip())
                    && (!internalHost(host) || host.equalsIgnoreCase(URI.create(publicUrl).getHost()))) throw new IllegalArgumentException();
            return base.resolve("/internal/v1/chat/sessions/");
        } catch (IllegalArgumentException error) { throw new IllegalArgumentException("Chat callback requires an internal service origin in production"); }
    }
    private static boolean internalHost(String host) {
        host = host.toLowerCase(Locale.ROOT);
        if (host.matches("[a-z][a-z0-9-]*") || host.endsWith(".internal") || host.endsWith(".local")
                || host.endsWith(".svc") || host.endsWith(".svc.cluster.local") || host.equals("[::1]") || host.matches("\\[f[cd][0-9a-f:]+\\]")) return true;
        if (!host.matches("[0-9]{1,3}(\\.[0-9]{1,3}){3}")) return false;
        String[] parts = host.split("\\.");
        for (String part : parts) if (Integer.parseInt(part) > 255) return false;
        int first = Integer.parseInt(parts[0]), second = Integer.parseInt(parts[1]);
        return first == 10 || first == 127 || (first == 192 && second == 168) || (first == 172 && second >= 16 && second <= 31);
    }
}
