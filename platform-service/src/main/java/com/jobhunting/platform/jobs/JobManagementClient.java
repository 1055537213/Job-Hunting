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

/** Java boundary for reading and managing the Python-owned job records. */
@Component
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class JobManagementClient {
    private static final int MAX_RESPONSE_BYTES = 4 * 1024 * 1024;
    private static final Logger LOG = LoggerFactory.getLogger(JobManagementClient.class);
    private static final Set<String> OPERATIONS = Set.of("list", "skill-requirements", "delete");
    private final URI base;
    private final String token;
    private final ObjectMapper mapper;
    private final HttpClient http;
    private final Semaphore slots = new Semaphore(8);

    public JobManagementClient(ObjectMapper mapper,
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
            throw new IllegalArgumentException("Invalid job management callback origin or credentials");
        }
        base = origin.resolve("/internal/v1/jobs/");
        this.token = token;
        this.mapper = mapper.rebuild().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
                .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS).build();
        http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(1)).proxy(ProxySelector.of(null))
                .followRedirects(HttpClient.Redirect.NEVER).version(HttpClient.Version.HTTP_1_1).build();
    }

    public JsonNode list(String session, long owner, String requestId) {
        return execute("list", session, owner, null, null, requestId);
    }

    public JsonNode updateSkillRequirements(String session, long owner, long jobId,
            JsonNode requirements, String requestId) {
        return execute("skill-requirements", session, owner, jobId, requirements, requestId);
    }

    public JsonNode delete(String session, long owner, long jobId, String requestId) {
        return execute("delete", session, owner, jobId, null, requestId);
    }

    private JsonNode execute(String operation, String session, long owner, Long jobId,
            JsonNode requirements, String requestId) {
        if (!OPERATIONS.contains(operation) || token.isBlank() || session == null || session.isBlank()
                || session.length() > 128 || owner <= 0 || "skill-requirements".equals(operation) && requirements == null
                || !"list".equals(operation) && (jobId == null || jobId <= 0)) throw invalid();
        if (!slots.tryAcquire()) throw unavailable();
        CompletableFuture<HttpResponse<byte[]>> pending = null;
        try {
            ObjectNode payload = mapper.createObjectNode().put("session_token", session);
            if (jobId != null) payload.put("job_id", jobId);
            if (requirements != null) payload.set("requirements", requirements.deepCopy());
            byte[] bytes = mapper.writeValueAsBytes(payload);
            if (bytes.length > MAX_RESPONSE_BYTES) throw invalid();
            var body = MultipartBodyPublisher.nonReplayable(HttpRequest.BodyPublishers.ofByteArray(bytes));
            var request = TraceHeaders.apply(HttpRequest.newBuilder(base.resolve(operation)), requestId)
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
            try {
                switch (operation) {
                    case "list" -> JobProjection.validateList(result);
                    case "skill-requirements" -> JobProjection.validateSkillUpdate(result);
                    case "delete" -> JobProjection.validateDelete(result);
                    default -> throw unavailable();
                }
            } catch (AuthException error) {
                throw unavailable();
            }
            return result;
        } catch (AuthException error) {
            LOG.warn("Job management callback rejected operation={} code={}", operation, error.code());
            throw error;
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw unavailable();
        } catch (Exception error) {
            LOG.warn("Job management callback failed operation={} type={}", operation, error.getClass().getSimpleName());
            throw unavailable();
        } finally {
            if (pending != null && !pending.isDone()) pending.cancel(true);
            slots.release();
        }
    }

    private static AuthException safeError(int status, String code) {
        String message = switch (status + ":" + code) {
            case "401:SESSION_EXPIRED" -> "登录状态已过期，请重新登录。";
            case "403:JOB_SCOPE_DENIED" -> "账号不可用或无权访问该职位。";
            case "404:JOB_NOT_FOUND" -> "职位不存在。";
            case "400:JOB_SKILL_REQUIREMENTS_INVALID" -> "职位技能分类无效，请检查后重试。";
            default -> null;
        };
        return message == null ? unavailable() : new AuthException(code, message, HttpStatus.valueOf(status));
    }

    static AuthException invalid() {
        return new AuthException("INVALID_REQUEST", "请求参数无效。", HttpStatus.UNPROCESSABLE_CONTENT);
    }

    static AuthException unavailable() {
        return new AuthException("JOB_MANAGEMENT_UNAVAILABLE", "职位管理结果暂未确认，请稍后重试。", HttpStatus.SERVICE_UNAVAILABLE);
    }

    private static boolean fields(JsonNode value, Set<String> keys) {
        return value != null && value.isObject() && value.propertyNames().equals(keys);
    }
}
