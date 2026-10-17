package com.jobhunting.platform.auth;

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
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicBoolean;

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
public class AccountExportClient {
    static final int MAX_BYTES = 16 * 1024 * 1024;
    static final Set<String> DIRECT = Set.of("candidate_profiles", "jobs", "long_texts", "chat_sessions", "chat_messages",
            "project_experience_cards", "resume_drafts", "knowledge_assets", "project_archive_imports", "project_collection_sessions",
            "visual_knowledge_items", "resume_artifacts", "usage_events", "tool_call_traces", "background_tasks", "account_balance_ledger", "recharge_orders");
    private static final Set<String> SECRET_FIELDS = Set.of("password_hash", "token_hash", "embedding", "session_token", "internal_token");
    private final URI endpoint;
    private final String token;
    private final int timeoutMs;
    private final ObjectMapper mapper;
    private final HttpClient http;
    private final Semaphore slots = new Semaphore(2);

    public AccountExportClient(ObjectMapper mapper,
            @Value("${platform.account-export.python-base-url:http://web:8000}") String baseUrl,
            @Value("${platform.internal-token:}") String token,
            @Value("${platform.account-export.connect-timeout-ms:1000}") int connectTimeoutMs,
            @Value("${platform.account-export.read-timeout-ms:30000}") int timeoutMs,
            @Value("${platform.web-security.environment:development}") String environment,
            @Value("${platform.web-security.public-base-url:http://localhost:8000}") String publicBaseUrl) {
        this.endpoint = endpoint(baseUrl, environment, publicBaseUrl);
        if (connectTimeoutMs < 1 || connectTimeoutMs > 1000 || timeoutMs < 1 || timeoutMs > 30000) {
            throw new IllegalArgumentException("Invalid export timeouts");
        }
        if (token.chars().anyMatch(c -> c < 33 || c > 126)
                || ("production".equalsIgnoreCase(environment.strip()) && token.length() < 32)) {
            throw new IllegalArgumentException("Invalid export service token");
        }
        this.mapper = mapper.rebuild().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
                .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS).build();
        this.token = token;
        this.timeoutMs = timeoutMs;
        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofMillis(connectTimeoutMs))
                .followRedirects(HttpClient.Redirect.NEVER).proxy(ProxySelector.of(null)).version(HttpClient.Version.HTTP_1_1).build();
    }

    public JsonNode export(String session, long accountId) {
        if (token.isBlank() || !slots.tryAcquire()) throw unavailable();
        CompletableFuture<HttpResponse<byte[]>> pending = null;
        try {
            byte[] payload = mapper.writeValueAsBytes(mapper.createObjectNode().put("session_token", session));
            var request = HttpRequest.newBuilder(endpoint).timeout(Duration.ofMillis(timeoutMs))
                    .header("X-Internal-Service-Token", token).header("Content-Type", "application/json")
                    .header("Accept", "application/json").POST(singleUseBody(payload)).build();
            pending = http.sendAsync(request, ignored -> new BoundedBody());
            var response = pending.get(timeoutMs, TimeUnit.MILLISECONDS);
            if (response.statusCode() == 413) {
                throw new AuthException("EXPORT_TOO_LARGE", "数据量超过同步导出限制，请联系管理员。", HttpStatus.CONTENT_TOO_LARGE);
            }
            if (response.statusCode() == 401) {
                throw new AuthException("SESSION_EXPIRED", "登录状态已过期，请重新登录。", HttpStatus.UNAUTHORIZED);
            }
            if (response.statusCode() != 200 || !response.headers().firstValue("Content-Type").orElse("")
                    .split(";", 2)[0].strip().equalsIgnoreCase("application/json")) throw unavailable();
            var result = mapper.readTree(response.body());
            validate(result, accountId);
            return result;
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            throw unavailable();
        } catch (ExecutionException | TimeoutException | tools.jackson.core.JacksonException | IllegalArgumentException invalid) {
            throw unavailable();
        } finally {
            if (pending != null && !pending.isDone()) pending.cancel(true);
            slots.release();
        }
    }

    static void validate(JsonNode value, long owner) {
        var fields = new HashSet<>(DIRECT);
        fields.addAll(Set.of("exported_at", "account", "consents", "account_balance", "knowledge_asset_versions", "project_archive_files", "project_collection_files"));
        if (!exactFields(value, fields) || !value.path("exported_at").isTextual()
                || !exactFields(value.get("account"), Set.of("id", "email", "display_name", "role", "status", "must_change_password",
                    "email_verified_at", "deleted_at", "created_at", "updated_at"))
                || !owner(value.get("account"), "id", owner) || !value.path("account").path("status").asText().equals("active")
                || !value.path("account").path("deleted_at").isNull()) throw unavailable();
        for (String table : DIRECT) {
            if (!value.get(table).isArray()) throw unavailable();
            for (var row : value.get(table)) if (!safeRow(row) || !owner(row, "account_id", owner)) throw unavailable();
        }
        if (!safeRow(value.get("account_balance")) || !owner(value.get("account_balance"), "account_id", owner)) throw unavailable();
        joined(value, "knowledge_asset_versions", "knowledge_assets", "asset_id");
        joined(value, "project_archive_files", "project_archive_imports", "project_archive_id");
        joined(value, "project_collection_files", "project_collection_sessions", "collection_id");
        if (!value.get("consents").isArray()) throw unavailable();
        for (var row : value.get("consents")) {
            if (!exactFields(row, Set.of("document_type", "version", "accepted_at"))) throw unavailable();
            for (var field : row) if (!field.isTextual()) throw unavailable();
        }
    }

    private static void joined(JsonNode value, String table, String parent, String field) {
        Set<Long> parents = new HashSet<>();
        for (var row : value.get(parent)) {
            if (!row.path("id").isIntegralNumber() || !row.path("id").canConvertToLong()) throw unavailable();
            parents.add(row.get("id").asLong());
        }
        if (!value.get(table).isArray()) throw unavailable();
        for (var row : value.get(table)) {
            if (!safeRow(row) || !row.path(field).isIntegralNumber() || !row.path(field).canConvertToLong()
                    || !parents.contains(row.get(field).asLong())) throw unavailable();
        }
    }

    private static boolean owner(JsonNode row, String key, long owner) {
        return row.path(key).isIntegralNumber() && row.path(key).canConvertToLong() && row.get(key).asLong() == owner;
    }

    private static boolean safeRow(JsonNode row) {
        return row != null && row.isObject() && row.propertyNames().stream().noneMatch(SECRET_FIELDS::contains);
    }

    private static boolean exactFields(JsonNode node, Set<String> fields) {
        return node != null && node.isObject() && node.propertyNames().equals(fields);
    }

    static AuthException unavailable() {
        return new AuthException("EXPORT_UNAVAILABLE", "账号导出暂时不可用，请稍后重试。", HttpStatus.SERVICE_UNAVAILABLE);
    }

    private static HttpRequest.BodyPublisher singleUseBody(byte[] bytes) {
        var body = HttpRequest.BodyPublishers.ofByteArray(bytes);
        var subscribed = new AtomicBoolean();
        return new HttpRequest.BodyPublisher() {
            @Override public long contentLength() { return bytes.length; }
            @Override public void subscribe(Flow.Subscriber<? super ByteBuffer> subscriber) {
                if (subscribed.compareAndSet(false, true)) body.subscribe(subscriber);
                else {
                    subscriber.onSubscribe(new Flow.Subscription() {
                        @Override public void request(long count) { }
                        @Override public void cancel() { }
                    });
                    subscriber.onError(new IOException("Export replay disabled"));
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
        @Override public void onSubscribe(Flow.Subscription subscription) { this.subscription = subscription; delegate.onSubscribe(subscription); }
        @Override public void onNext(List<ByteBuffer> buffers) {
            if (failed) return;
            for (ByteBuffer buffer : buffers) received += buffer.remaining();
            if (received > MAX_BYTES) {
                failed = true;
                subscription.cancel();
                delegate.onError(new IOException("Export response too large"));
            } else delegate.onNext(buffers);
        }
        @Override public void onError(Throwable error) { if (!failed) delegate.onError(error); }
        @Override public void onComplete() { if (!failed) delegate.onComplete(); }
    }

    private static URI endpoint(String baseUrl, String environment, String publicBaseUrl) {
        try {
            var base = URI.create(baseUrl);
            String host = base.getHost();
            if (!("http".equals(base.getScheme()) || "https".equals(base.getScheme())) || host == null
                    || base.getRawUserInfo() != null || base.getRawQuery() != null || base.getRawFragment() != null
                    || !(base.getRawPath().isEmpty() || "/".equals(base.getRawPath())) || base.getPort() == 0 || base.getPort() > 65535) {
                throw new IllegalArgumentException();
            }
            if ("production".equalsIgnoreCase(environment.strip())
                    && (!internalHost(host) || host.equalsIgnoreCase(URI.create(publicBaseUrl).getHost()))) throw new IllegalArgumentException();
            return base.resolve("/internal/v1/accounts/export");
        } catch (IllegalArgumentException invalid) {
            throw new IllegalArgumentException("Export base URL must be a private service origin in production");
        }
    }

    private static boolean internalHost(String host) {
        host = host.toLowerCase(Locale.ROOT);
        if (host.matches("[a-z][a-z0-9-]*") || host.endsWith(".internal") || host.endsWith(".local")
                || host.endsWith(".svc") || host.endsWith(".svc.cluster.local") || host.equals("[::1]")
                || host.matches("\\[f[cd][0-9a-f:]+\\]")) return true;
        if (!host.matches("[0-9]{1,3}(\\.[0-9]{1,3}){3}")) return false;
        String[] octets = host.split("\\.");
        for (String octet : octets) if (Integer.parseInt(octet) > 255) return false;
        int first = Integer.parseInt(octets[0]), second = Integer.parseInt(octets[1]);
        return first == 10 || first == 127 || (first == 192 && second == 168) || (first == 172 && second >= 16 && second <= 31);
    }
}
