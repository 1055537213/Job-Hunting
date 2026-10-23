package com.jobhunting.platform.profiles;

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

@Component
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class ProfileQueryClient {
    static final int MAX_BYTES = 4 * 1024 * 1024;
    static final Set<String> FIELDS = Set.of("id", "name", "status", "education", "experience_years", "skills",
            "preferred_cities", "acceptable_cities", "salary_floor_k", "expected_salary_k", "target_directions",
            "unacceptable", "preference_weights");
    private final ObjectMapper mapper;
    private final HttpClient http;
    private final URI endpoint;
    private final String token;
    private final int timeoutMs;
    private final Semaphore slots = new Semaphore(8);

    public ProfileQueryClient(ObjectMapper mapper,
            @Value("${platform.profiles.python-base-url:http://web:8000}") String baseUrl,
            @Value("${platform.internal-token:}") String token,
            @Value("${platform.profiles.connect-timeout-ms:1000}") int connectMs,
            // The Python callback performs a nested Java account lookup with its own
            // five-second deadline, so the outer profile request needs a larger budget.
            @Value("${platform.profiles.read-timeout-ms:10000}") int timeoutMs,
            @Value("${platform.web-security.environment:development}") String environment,
            @Value("${platform.web-security.public-base-url:http://localhost:8000}") String publicBaseUrl) {
        this.endpoint = endpoint(baseUrl, environment, publicBaseUrl);
        if (connectMs < 1 || connectMs > 1000 || timeoutMs < 1 || timeoutMs > 10000
                || token.chars().anyMatch(c -> c < 33 || c > 126)
                || ("production".equalsIgnoreCase(environment.strip()) && token.length() < 32)) {
            throw new IllegalArgumentException("Invalid profile callback settings");
        }
        this.mapper = mapper.rebuild().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
                .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS).build();
        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofMillis(connectMs)).proxy(ProxySelector.of(null))
                .followRedirects(HttpClient.Redirect.NEVER).version(HttpClient.Version.HTTP_1_1).build();
        this.token = token;
        this.timeoutMs = timeoutMs;
    }

    public JsonNode query(String session, long accountId, Long candidateId, String requestId) {
        var payload = mapper.createObjectNode().put("session_token", session);
        if (candidateId == null) payload.putNull("candidate_id"); else payload.put("candidate_id", candidateId);
        var value = exchange(payload, "query", requestId);
        String key = candidateId == null ? "profiles" : "profile";
        if (!fields(value, Set.of("account_id", key)) || !id(value.get("account_id")) || value.get("account_id").asLong() != accountId) throw unavailable();
        if (candidateId == null) {
            if (!value.get(key).isArray()) throw unavailable();
            var ids = new HashSet<Long>();
            for (var row : value.get(key)) {
                validateProfile(row);
                if (!ids.add(row.get("id").asLong())) throw unavailable();
            }
        } else {
            validateProfile(value.get(key));
            if (value.get(key).get("id").asLong() != candidateId) throw unavailable();
        }
        return mapper.createObjectNode().set(key, value.get(key));
    }

    public JsonNode create(String session, long accountId, String key, JsonNode profile, String requestId) {
        var payload = mapper.createObjectNode().put("session_token", session).put("idempotency_key", key);
        payload.set("profile", profile);
        try {
            var value = exchange(payload, "create", requestId);
            if (!fields(value, Set.of("account_id", "candidate_id", "profile")) || !id(value.get("account_id"))
                    || value.get("account_id").asLong() != accountId || !id(value.get("candidate_id"))) throw createUnavailable();
            validateProfile(value.get("profile"));
            if (value.get("candidate_id").asLong() != value.get("profile").get("id").asLong()) throw createUnavailable();
            return mapper.createObjectNode().put("candidate_id", value.get("candidate_id").asLong()).set("profile", value.get("profile"));
        } catch (AuthException error) {
            if ("PROFILE_QUERY_UNAVAILABLE".equals(error.code())) throw createUnavailable();
            throw error;
        }
    }

    public JsonNode delete(String session, long accountId, long candidateId, String requestId) {
        try {
            var value = exchange(mapper.createObjectNode().put("session_token", session).put("candidate_id", candidateId), "delete", requestId);
            if (!fields(value, Set.of("account_id", "candidate_id", "deleted", "cleanup_status", "rag_cleanup"))
                    || !id(value.get("account_id")) || value.get("account_id").asLong() != accountId
                    || !id(value.get("candidate_id")) || value.get("candidate_id").asLong() != candidateId
                    || !value.get("deleted").isBoolean() || !value.get("deleted").asBoolean()
                    || !value.get("cleanup_status").isTextual() || !Set.of("pending", "completed").contains(value.get("cleanup_status").asText())
                    || !value.get("rag_cleanup").isTextual() || !"database_cascade".equals(value.get("rag_cleanup").asText())) throw deleteUnavailable();
            var result = (tools.jackson.databind.node.ObjectNode) value.deepCopy();
            result.remove("account_id");
            return result;
        } catch (AuthException error) {
            if ("PROFILE_QUERY_UNAVAILABLE".equals(error.code())) throw deleteUnavailable();
            throw error;
        }
    }

    private JsonNode exchange(JsonNode payload, String operation, String requestId) {
        if (token.isBlank() || !slots.tryAcquire()) throw unavailable();
        CompletableFuture<HttpResponse<byte[]>> pending = null;
        try {
            var request = TraceHeaders.apply(HttpRequest.newBuilder(endpoint.resolve(operation)), requestId).timeout(Duration.ofMillis(timeoutMs))
                    .header("Content-Type", "application/json").header("Accept", "application/json")
                    .header("X-Internal-Service-Token", token)
                    .POST(singleUse(mapper.writeValueAsBytes(payload))).build();
            pending = http.sendAsync(request, ignored -> new BoundedBody());
            var response = pending.get(timeoutMs, TimeUnit.MILLISECONDS);
            if (!response.headers().firstValue("Content-Type").orElse("").split(";", 2)[0].strip().equalsIgnoreCase("application/json")) throw unavailable();
            var value = mapper.readTree(response.body());
            if (response.statusCode() != 200) {
                if (!fields(value, Set.of("code", "detail")) || !value.get("detail").isTextual() || !value.get("code").isTextual()) throw unavailable();
                var safe = switch (operation) {
                    case "create" -> createError(response.statusCode(), value.get("code").asText());
                    case "delete" -> deleteError(response.statusCode(), value.get("code").asText());
                    default -> safeError(response.statusCode());
                };
                if (!safe.code().equals(value.get("code").asText())
                        || (response.statusCode() == 404 && ("create".equals(operation) || payload.get("candidate_id").isNull()))) throw unavailable();
                throw safe;
            }
            return value;
        } catch (AuthException error) {
            throw error;
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw unavailable();
        } catch (Exception error) {
            // Never reflect a transport error, token, upstream body or personal field.
            throw unavailable();
        } finally {
            if (pending != null && !pending.isDone()) pending.cancel(true);
            slots.release();
        }
    }

    private static void validateProfile(JsonNode row) {
        if (!fields(row, FIELDS) || !id(row.get("id"))) throw unavailable();
        for (String key : List.of("name", "status", "education")) if (!row.get(key).isTextual()) throw unavailable();
        if (!number(row.get("experience_years"))) throw unavailable();
        for (String key : List.of("salary_floor_k", "expected_salary_k")) {
            if (!(row.get(key).isNull() || (row.get(key).isIntegralNumber() && row.get(key).canConvertToLong()))) throw unavailable();
        }
        for (String key : List.of("preferred_cities", "acceptable_cities", "target_directions", "unacceptable")) {
            if (!row.get(key).isArray()) throw unavailable();
            for (var item : row.get(key)) if (!item.isTextual()) throw unavailable();
        }
        if (!row.get("skills").isObject() || !row.get("preference_weights").isObject()) throw unavailable();
        for (var item : row.get("skills")) if (!item.isTextual()) throw unavailable();
        for (var item : row.get("preference_weights")) if (!number(item)) throw unavailable();
    }

    private static boolean number(JsonNode node) { return node.isNumber() && Double.isFinite(node.asDouble()); }
    private static boolean id(JsonNode node) { return node != null && node.isIntegralNumber() && node.canConvertToLong() && node.asLong() > 0; }
    private static boolean fields(JsonNode node, Set<String> fields) { return node != null && node.isObject() && node.propertyNames().equals(fields); }

    static AuthException unavailable() {
        return new AuthException("PROFILE_QUERY_UNAVAILABLE", "档案查询暂时不可用，请稍后重试。", HttpStatus.SERVICE_UNAVAILABLE);
    }

    static AuthException createUnavailable() {
        return new AuthException("PROFILE_CREATE_UNAVAILABLE", "档案创建结果暂未确认，请使用相同请求重试。", HttpStatus.SERVICE_UNAVAILABLE);
    }

    static AuthException deleteUnavailable() {
        return new AuthException("PROFILE_DELETE_UNAVAILABLE", "档案删除结果暂未确认，请使用相同档案重试。", HttpStatus.SERVICE_UNAVAILABLE);
    }

    private static AuthException deleteError(int status, String code) {
        if (status == 401 || status == 403 || status == 404) return safeError(status);
        if (status == 409 && "PROFILE_DELETE_BUSY".equals(code))
            return new AuthException(code, "当前仍有请求或后台任务执行，请稍后再删除档案。", HttpStatus.CONFLICT);
        if (status == 422 && "INVALID_REQUEST".equals(code))
            return new AuthException(code, "请求参数无效。", HttpStatus.UNPROCESSABLE_CONTENT);
        return deleteUnavailable();
    }

    private static AuthException createError(int status, String code) {
        if (status == 401 || status == 403) return safeError(status);
        if (status == 409 && "DUPLICATE_RESOURCE".equals(code))
            return new AuthException(code, "候选人档案已存在，请勿重复创建。", HttpStatus.CONFLICT);
        if (status == 409 && "PROFILE_CREATE_CONFLICT".equals(code))
            return new AuthException(code, "创建请求已失效或内容已变更，请重新发起创建。", HttpStatus.CONFLICT);
        if ((status == 400 || status == 422) && "INVALID_REQUEST".equals(code))
            return new AuthException(code, "请求参数无效。", HttpStatus.valueOf(status));
        if (status == 413 && "PROFILE_CREATE_TOO_LARGE".equals(code))
            return new AuthException(code, "档案内容过大，请精简后重试。", HttpStatus.CONTENT_TOO_LARGE);
        return createUnavailable();
    }

    private static AuthException safeError(int status) {
        return switch (status) {
            case 401 -> new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
            case 403 -> new AuthException("ACCOUNT_DISABLED", "账号已被停用，请重新登录。", HttpStatus.FORBIDDEN);
            case 404 -> new AuthException("PROFILE_NOT_FOUND", "候选人档案不存在。", HttpStatus.NOT_FOUND);
            case 413 -> new AuthException("PROFILE_QUERY_TOO_LARGE", "档案数据超出同步查询限制，请联系管理员。", HttpStatus.CONTENT_TOO_LARGE);
            default -> unavailable();
        };
    }

    private static HttpRequest.BodyPublisher singleUse(byte[] bytes) {
        var body = HttpRequest.BodyPublishers.ofByteArray(bytes);
        var used = new AtomicBoolean();
        return new HttpRequest.BodyPublisher() {
            @Override public long contentLength() { return bytes.length; }
            @Override public void subscribe(Flow.Subscriber<? super ByteBuffer> subscriber) {
                if (used.compareAndSet(false, true)) body.subscribe(subscriber);
                else {
                    subscriber.onSubscribe(new Flow.Subscription() {
                        @Override public void request(long n) { }
                        @Override public void cancel() { }
                    });
                    subscriber.onError(new IOException("Profile callback replay disabled"));
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
                failed = true;
                subscription.cancel();
                delegate.onError(new IOException("Profile callback response too large"));
            } else delegate.onNext(buffers);
        }
        @Override public void onError(Throwable error) { if (!failed) delegate.onError(error); }
        @Override public void onComplete() { if (!failed) delegate.onComplete(); }
    }

    private static URI endpoint(String url, String environment, String publicUrl) {
        try {
            var base = URI.create(url);
            String host = base.getHost();
            if (!("http".equals(base.getScheme()) || "https".equals(base.getScheme())) || host == null
                    || base.getRawUserInfo() != null || base.getRawQuery() != null || base.getRawFragment() != null
                    || !(base.getRawPath().isEmpty() || "/".equals(base.getRawPath())) || base.getPort() == 0 || base.getPort() > 65535) throw new IllegalArgumentException();
            if ("production".equalsIgnoreCase(environment.strip())
                    && (!internalHost(host) || host.equalsIgnoreCase(URI.create(publicUrl).getHost()))) throw new IllegalArgumentException();
            return base.resolve("/internal/v1/profiles/query");
        } catch (IllegalArgumentException error) {
            throw new IllegalArgumentException("Profile callback requires an internal service origin in production");
        }
    }

    private static boolean internalHost(String host) {
        host = host.toLowerCase(Locale.ROOT);
        if (host.matches("[a-z][a-z0-9-]*") || host.endsWith(".internal") || host.endsWith(".local")
                || host.endsWith(".svc") || host.endsWith(".svc.cluster.local") || host.equals("[::1]") || host.matches("\\[f[cd][0-9a-f:]+\\]")) return true;
        if (!host.matches("[0-9]{1,3}(\\.[0-9]{1,3}){3}")) return false;
        String[] octets = host.split("\\.");
        for (String octet : octets) if (Integer.parseInt(octet) > 255) return false;
        int first = Integer.parseInt(octets[0]), second = Integer.parseInt(octets[1]);
        return first == 10 || first == 127 || (first == 192 && second == 168) || (first == 172 && second >= 16 && second <= 31);
    }
}
