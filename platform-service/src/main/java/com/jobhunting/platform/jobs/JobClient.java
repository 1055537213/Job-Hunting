package com.jobhunting.platform.jobs;

import java.net.ProxySelector;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.UUID;
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
import org.springframework.web.multipart.MultipartFile;
import tools.jackson.core.StreamReadFeature;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

@Component
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class JobClient {
    static final int MAX_FILES = 4;
    static final long MAX_FILE_BYTES = 8L * 1024 * 1024;
    static final long MAX_TOTAL_BYTES = 20L * 1024 * 1024;
    private static final int MAX_RESPONSE_BYTES = 4 * 1024 * 1024;
    private static final Logger LOG = LoggerFactory.getLogger(JobClient.class);
    private final URI base;
    private final String token;
    private final ObjectMapper mapper;
    private final HttpClient http;
    private final Semaphore slots = new Semaphore(2);

    public JobClient(ObjectMapper mapper,
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
            throw new IllegalArgumentException("Invalid job screenshot callback origin or credentials");
        }
        base = origin.resolve("/internal/v1/jobs/");
        this.token = token;
        this.mapper = mapper.rebuild().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
                .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS).build();
        http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(1)).proxy(ProxySelector.of(null))
                .followRedirects(HttpClient.Redirect.NEVER).version(HttpClient.Version.HTTP_1_1).build();
    }

    public JsonNode upload(String session, long owner, List<MultipartFile> files, String sourceUrl, String requestId) {
        if (token.isBlank() || session == null || session.isBlank() || session.length() > 128
                || owner <= 0 || files == null || files.isEmpty() || files.size() > MAX_FILES) throw invalid();
        long total = 0;
        for (MultipartFile file : files) {
            long size = file.getSize();
            if (size <= 0 || size > MAX_FILE_BYTES) throw new AuthException("JOB_SCREENSHOT_TOO_LARGE",
                    "单张职位截图不能超过 8 MB 且不能为空。", HttpStatus.PAYLOAD_TOO_LARGE);
            total = Math.addExact(total, size);
        }
        if (total > MAX_TOTAL_BYTES) throw new AuthException("JOB_SCREENSHOT_TOO_LARGE",
                "职位截图总大小不能超过 20 MB。", HttpStatus.PAYLOAD_TOO_LARGE);
        if (!slots.tryAcquire()) throw unavailable();
        CompletableFuture<HttpResponse<byte[]>> pending = null;
        try {
            String boundary = "jobagent-" + UUID.randomUUID().toString().replace("-", "");
            List<MultipartBodyPublisher.FilePart> parts = new ArrayList<>();
            for (MultipartFile file : files) {
                parts.add(new MultipartBodyPublisher.FilePart("screenshots", safeFilename(file), file.getContentType(), file));
            }
            List<MultipartBodyPublisher.Field> formFields = new ArrayList<>();
            formFields.add(new MultipartBodyPublisher.Field("session_token", session, "text/plain"));
            if (sourceUrl != null && !sourceUrl.isBlank()) formFields.add(new MultipartBodyPublisher.Field("source_url", sourceUrl, "text/plain"));
            var body = MultipartBodyPublisher.nonReplayable(MultipartBodyPublisher.publisher(boundary, formFields, parts));
            var request = TraceHeaders.apply(HttpRequest.newBuilder(base.resolve("screenshots")), requestId)
                    .timeout(Duration.ofSeconds(300)).header("Content-Type", "multipart/form-data; boundary=" + boundary)
                    .header("Accept", "application/json").header("X-Internal-Service-Token", token).POST(body).build();
            pending = http.sendAsync(request, ignored -> HttpResponse.BodySubscribers.ofByteArray());
            var response = pending.get(300, TimeUnit.SECONDS);
            if (!response.headers().firstValue("Content-Type").orElse("").split(";", 2)[0].strip().equalsIgnoreCase("application/json")) throw unavailable();
            var value = mapper.readTree(response.body());
            if (response.statusCode() != 200) {
                if (!fields(value, Set.of("code", "detail")) || !value.path("code").isTextual() || !value.path("detail").isTextual()) throw unavailable();
                throw safeError(response.statusCode(), value.get("code").asText());
            }
            if (value == null || !value.isObject() || !id(value.get("account_id")) || value.get("account_id").asLong() != owner) throw unavailable();
            var result = (ObjectNode) value.deepCopy();
            result.remove("account_id");
            JobProjection.validate(result);
            return result;
        } catch (AuthException error) { LOG.warn("Job screenshot callback rejected code={}", error.code()); throw error;
        } catch (InterruptedException error) { Thread.currentThread().interrupt(); throw unavailable();
        } catch (Exception error) { LOG.warn("Job screenshot callback failed type={}", error.getClass().getSimpleName()); throw unavailable();
        } finally { if (pending != null && !pending.isDone()) pending.cancel(true); slots.release(); }
    }

    private static String safeFilename(MultipartFile file) {
        String value = file.getOriginalFilename();
        if (value == null || value.isBlank()) return "screenshot";
        value = value.replace('\\', '/');
        int slash = value.lastIndexOf('/');
        return slash >= 0 ? value.substring(slash + 1) : value;
    }
    static boolean id(JsonNode value) { return value != null && value.isIntegralNumber() && value.canConvertToLong() && value.asLong() > 0; }
    static boolean fields(JsonNode value, Set<String> keys) { return value != null && value.isObject() && value.propertyNames().equals(keys); }
    static AuthException invalid() { return new AuthException("INVALID_REQUEST", "请求参数无效。", HttpStatus.UNPROCESSABLE_CONTENT); }
    static AuthException unavailable() { return new AuthException("JOB_SCREENSHOT_UNAVAILABLE", "职位截图识别结果暂未确认，请稍后重试。", HttpStatus.SERVICE_UNAVAILABLE); }

    private static AuthException safeError(int status, String code) {
        String message = switch (status + ":" + code) {
            case "401:SESSION_EXPIRED" -> "登录状态已过期，请重新登录。";
            case "409:DUPLICATE_RESOURCE" -> "相同职位已经导入，请勿重复提交。";
            case "409:JOB_BUSY" -> "当前仍有请求执行，请稍后重试。";
            case "400:JOB_SCREENSHOT_INVALID" -> "职位截图无效，请更换图片后重试。";
            case "400:JOB_NOT_RECOGNIZED" -> "图片未被可靠识别为招聘职位，请改用文本导入。";
            case "413:JOB_SCREENSHOT_TOO_LARGE" -> "职位截图超过上传限制。";
            case "422:FILE_INFECTED" -> "文件未通过安全扫描，请更换文件后重试。";
            case "429:RATE_LIMITED" -> "请求过于频繁，请稍后重试。";
            case "502:JOB_SCREENSHOT_MODEL_ERROR" -> "职位截图识别服务返回异常，请稍后重试。";
            case "503:FILE_SCANNER_UNAVAILABLE" -> "文件安全扫描暂时不可用，请稍后重试。";
            default -> null;
        };
        return message == null ? unavailable() : new AuthException(code, message, HttpStatus.valueOf(status));
    }
}
