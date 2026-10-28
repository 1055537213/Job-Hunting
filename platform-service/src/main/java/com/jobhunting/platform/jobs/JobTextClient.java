package com.jobhunting.platform.jobs;

import java.net.ProxySelector;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import com.jobhunting.platform.auth.AuthException;
import com.jobhunting.platform.observability.TraceHeaders;
import com.jobhunting.platform.transport.MultipartBodyPublisher;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import tools.jackson.core.StreamReadFeature;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** Java boundary for ordinary user-pasted job text. */
@Component
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class JobTextClient {
    static final int MAX_RAW_TEXT_CHARS = 65_536;
    static final int MAX_SOURCE_URL_CHARS = 2_048;
    private static final int MAX_RESPONSE_BYTES = 4 * 1024 * 1024;
    private static final Logger LOG = LoggerFactory.getLogger(JobTextClient.class);
    private final URI endpoint;
    private final String token;
    private final ObjectMapper mapper;
    private final HttpClient http;
    private final Semaphore slots = new Semaphore(8);

    public JobTextClient(ObjectMapper mapper,
            @Value("${platform.jobs.python-base-url:http://web:8000}") String url,
            @Value("${platform.internal-token:}") String token,
            @Value("${platform.web-security.environment:development}") String environment,
            @Value("${platform.web-security.public-base-url:http://localhost:8000}") String publicUrl) {
        var origin = URI.create(url);
        if (!("http".equals(origin.getScheme()) || "https".equals(origin.getScheme())) || origin.getHost() == null
                || origin.getUserInfo() != null || origin.getQuery() != null || origin.getFragment() != null
                || !Set.of("", "/").contains(origin.getRawPath()) || origin.getPort() == 0 || origin.getPort() > 65535
                || token.chars().anyMatch(c -> c < 33 || c > 126)
                || ("production".equalsIgnoreCase(environment.strip()) && (token.length() < 32
                    || !Set.of("web", "localhost", "127.0.0.1", "[::1]").contains(origin.getHost())
                    || origin.getHost().equalsIgnoreCase(URI.create(publicUrl).getHost())))) {
            throw new IllegalArgumentException("Invalid job text callback origin or credentials");
        }
        endpoint = origin.resolve("/internal/v1/jobs/text");
        this.token = token;
        this.mapper = mapper.rebuild().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
                .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS).build();
        http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(1)).proxy(ProxySelector.of(null))
                .followRedirects(HttpClient.Redirect.NEVER).version(HttpClient.Version.HTTP_1_1).build();
    }

    public JsonNode importText(String session, long owner, String rawText, String sourceUrl, String requestId) {
        if (token.isBlank() || session == null || session.isBlank() || session.length() > 128 || owner <= 0
                || rawText == null || rawText.isBlank() || rawText.length() > MAX_RAW_TEXT_CHARS
                || sourceUrl != null && sourceUrl.length() > MAX_SOURCE_URL_CHARS) throw invalid();
        if (!slots.tryAcquire()) throw unavailable();
        CompletableFuture<HttpResponse<byte[]>> pending = null;
        try {
            ObjectNode payload = mapper.createObjectNode().put("session_token", session).put("raw_text", rawText);
            if (sourceUrl == null || sourceUrl.isBlank()) payload.putNull("source_url");
            else payload.put("source_url", sourceUrl);
            var bytes = mapper.writeValueAsBytes(payload);
            if (bytes.length > MAX_RESPONSE_BYTES) throw invalid();
            var body = MultipartBodyPublisher.nonReplayable(HttpRequest.BodyPublishers.ofByteArray(bytes));
            var request = TraceHeaders.apply(HttpRequest.newBuilder(endpoint), requestId)
                    .timeout(Duration.ofSeconds(30)).header("Content-Type", "application/json")
                    .header("Accept", "application/json").header("X-Internal-Service-Token", token).POST(body).build();
            pending = http.sendAsync(request, ignored -> HttpResponse.BodySubscribers.ofByteArray());
            var response = pending.get(30, TimeUnit.SECONDS);
            if (response.body().length > MAX_RESPONSE_BYTES
                    || !response.headers().firstValue("Content-Type").orElse("").split(";", 2)[0].strip().equalsIgnoreCase("application/json")) {
                throw unavailable();
            }
            var value = mapper.readTree(response.body());
            if (response.statusCode() != 200) {
                if (!fields(value, Set.of("code", "detail")) || !value.path("code").isTextual()
                        || !value.path("detail").isTextual()) throw unavailable();
                throw safeError(response.statusCode(), value.get("code").asText());
            }
            if (value == null || !value.isObject() || !JobClient.id(value.get("account_id"))
                    || value.get("account_id").asLong() != owner) throw unavailable();
            var result = (ObjectNode) value.deepCopy();
            result.remove("account_id");
            JobProjection.validateText(result);
            return result;
        } catch (AuthException error) {
            LOG.warn("Job text callback rejected code={}", error.code());
            throw error;
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw unavailable();
        } catch (Exception error) {
            LOG.warn("Job text callback failed type={}", error.getClass().getSimpleName());
            throw unavailable();
        } finally {
            if (pending != null && !pending.isDone()) pending.cancel(true);
            slots.release();
        }
    }

    static AuthException invalid() {
        return new AuthException("INVALID_REQUEST", "请求参数无效。", HttpStatus.UNPROCESSABLE_CONTENT);
    }

    static AuthException unavailable() {
        return new AuthException("JOB_TEXT_UNAVAILABLE", "职位文本导入结果暂未确认，请稍后重试。", HttpStatus.SERVICE_UNAVAILABLE);
    }

    private static boolean fields(JsonNode value, Set<String> keys) {
        return value != null && value.isObject() && value.propertyNames().equals(keys);
    }

    private static AuthException safeError(int status, String code) {
        String message = switch (status + ":" + code) {
            case "401:SESSION_EXPIRED" -> "登录状态已过期，请重新登录。";
            case "403:JOB_SCOPE_DENIED" -> "账号不可用或无权访问该职位。";
            case "400:JOB_TEXT_INVALID" -> "职位文本无效，请补充完整招聘信息。";
            case "409:DUPLICATE_RESOURCE" -> "相同职位已经导入，请勿重复提交。";
            default -> null;
        };
        return message == null ? unavailable() : new AuthException(code, message, HttpStatus.valueOf(status));
    }
}
