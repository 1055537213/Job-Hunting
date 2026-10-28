package com.jobhunting.platform.resumes;

import java.net.ProxySelector;
import java.nio.ByteBuffer;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.Flow;
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
public class ResumeClient {
    static final long MAX_FILE_BYTES = 20L * 1024 * 1024;
    private static final int MAX_RESPONSE_BYTES = 4 * 1024 * 1024;
    private static final Logger LOG = LoggerFactory.getLogger(ResumeClient.class);
    private final URI base;
    private final String token;
    private final ObjectMapper mapper;
    private final HttpClient http;
    private final Semaphore slots = new Semaphore(4);

    public ResumeClient(ObjectMapper mapper,
            @Value("${platform.resumes.python-base-url:http://web:8000}") String url,
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
            throw new IllegalArgumentException("Invalid resume callback origin or credentials");
        }
        base = origin.resolve("/internal/v1/resumes/");
        this.token = token;
        this.mapper = mapper.rebuild().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
                .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS).build();
        http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(1)).proxy(ProxySelector.of(null))
                .followRedirects(HttpClient.Redirect.NEVER).version(HttpClient.Version.HTTP_1_1).build();
    }

    public JsonNode upload(String session, long owner, long candidateId, MultipartFile file, String requestId) {
        if (token.isBlank() || session == null || session.isBlank() || session.length() > 128
                || owner <= 0 || candidateId <= 0 || file == null) throw invalid();
        long size = file.getSize();
        if (size <= 0 || size > MAX_FILE_BYTES) {
            throw new AuthException("RESUME_UPLOAD_TOO_LARGE", "简历文件不能超过 20 MB 且不能为空。", HttpStatus.PAYLOAD_TOO_LARGE);
        }
        if (!slots.tryAcquire()) throw unavailable();
        CompletableFuture<HttpResponse<byte[]>> pending = null;
        try {
            String boundary = "jobagent-" + java.util.UUID.randomUUID().toString().replace("-", "");
            var body = MultipartBodyPublisher.nonReplayable(MultipartBodyPublisher.publisher(boundary,
                    List.of(new MultipartBodyPublisher.Field("session_token", session, "text/plain"),
                            new MultipartBodyPublisher.Field("candidate_id", Long.toString(candidateId), "text/plain")),
                    List.of(new MultipartBodyPublisher.FilePart("file", safeFilename(file),
                            file.getContentType(), file))));
            var request = TraceHeaders.apply(HttpRequest.newBuilder(base.resolve("upload")), requestId)
                    .timeout(Duration.ofSeconds(300)).header("Content-Type", "multipart/form-data; boundary=" + boundary)
                    .header("Accept", "application/json").header("X-Internal-Service-Token", token).POST(body).build();
            pending = http.sendAsync(request, ignored -> new BoundedBody(MAX_RESPONSE_BYTES));
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
            ResumeProjection.validate(result, candidateId);
            sanitize(result);
            return result;
        } catch (AuthException error) { LOG.warn("Resume callback rejected code={}", error.code()); throw error;
        } catch (InterruptedException error) { Thread.currentThread().interrupt(); throw unavailable();
        } catch (Exception error) { LOG.warn("Resume callback failed type={}", error.getClass().getSimpleName()); throw unavailable();
        } finally { if (pending != null && !pending.isDone()) pending.cancel(true); slots.release(); }
    }

    private static String safeFilename(MultipartFile file) {
        String value = file.getOriginalFilename();
        if (value == null || value.isBlank()) return "resume";
        value = value.replace('\\', '/');
        int slash = value.lastIndexOf('/');
        if (slash >= 0) value = value.substring(slash + 1);
        return value.isBlank() ? "resume" : value;
    }
    static boolean id(JsonNode value) { return value != null && value.isIntegralNumber() && value.canConvertToLong() && value.asLong() > 0; }
    static boolean fields(JsonNode value, Set<String> keys) { return value != null && value.isObject() && value.propertyNames().equals(keys); }
    static AuthException invalid() { return new AuthException("INVALID_REQUEST", "请求参数无效。", HttpStatus.UNPROCESSABLE_CONTENT); }
    static AuthException unavailable() { return new AuthException("RESUME_UNAVAILABLE", "简历上传结果暂未确认，请刷新简历列表核实后重试。", HttpStatus.SERVICE_UNAVAILABLE); }

    private void sanitize(ObjectNode result) {
        if (result.get("warning").isTextual()) {
            result.put("warning", "简历已保存，但后续处理暂未完成，请查看任务状态或稍后重试。");
        }
        if (result.get("rag_update").isObject()) {
            var update = mapper.createObjectNode();
            for (String key : List.of("document_count", "chunk_count", "mode")) {
                if (result.get("rag_update").has(key)) update.set(key, result.get("rag_update").get(key));
            }
            result.set("rag_update", update);
        }
        if (result.get("task").isObject()) {
            var task = (ObjectNode) result.get("task");
            task.putNull("result");
            task.put("error_summary", task.get("error_summary").isTextual()
                    ? "后台任务未完成，请查看任务状态。" : null);
        }
        var artifact = (ObjectNode) result.get("artifact");
        artifact.putNull("scan_engine");
        artifact.putNull("scan_reason");
    }

    private static AuthException safeError(int status, String code) {
        String message = switch (status + ":" + code) {
            case "401:SESSION_EXPIRED" -> "登录状态已过期，请重新登录。";
            case "403:RESUME_SCOPE_DENIED" -> "账号不可用或无权访问该档案。";
            case "404:PROFILE_NOT_FOUND" -> "候选人档案不存在。";
            case "409:DUPLICATE_RESUME" -> "相同简历已经上传，请勿重复提交。";
            case "409:RESUME_BUSY" -> "当前仍有请求或任务执行，请稍后重试。";
            case "400:RESUME_INVALID" -> "简历文件无效或格式不受支持。";
            case "413:RESUME_UPLOAD_TOO_LARGE" -> "简历文件不能超过 20 MB 且不能为空。";
            case "422:FILE_INFECTED" -> "文件未通过安全扫描，请更换文件后重试。";
            case "503:FILE_SCANNER_UNAVAILABLE" -> "文件安全扫描暂时不可用，请稍后重试。";
            default -> null;
        };
        return message == null ? unavailable() : new AuthException(code, message, HttpStatus.valueOf(status));
    }

    private static final class BoundedBody implements HttpResponse.BodySubscriber<byte[]> {
        private final HttpResponse.BodySubscriber<byte[]> delegate = HttpResponse.BodySubscribers.ofByteArray();
        private final int limit;
        private Flow.Subscription subscription;
        private long size;
        private boolean failed;

        private BoundedBody(int limit) { this.limit = limit; }
        @Override public CompletionStage<byte[]> getBody() { return delegate.getBody(); }
        @Override public void onSubscribe(Flow.Subscription value) { subscription = value; delegate.onSubscribe(value); }
        @Override public void onNext(List<ByteBuffer> buffers) {
            if (failed) return;
            for (var buffer : buffers) size += buffer.remaining();
            if (size > limit) {
                failed = true;
                subscription.cancel();
                delegate.onError(new java.io.IOException("Resume response too large"));
            } else delegate.onNext(buffers);
        }
        @Override public void onError(Throwable error) { if (!failed) delegate.onError(error); }
        @Override public void onComplete() { if (!failed) delegate.onComplete(); }
    }
}
