package com.jobhunting.platform.observability;

import java.io.IOException;
import java.net.ProxySelector;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.time.Duration;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Flow;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;
import tools.jackson.core.StreamReadFeature;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

@Component
@ConditionalOnProperty(name = "platform.auth.enabled", havingValue = "true")
public class PythonObservabilityClient {
    private static final int MAX_RESPONSE_BYTES = 1024 * 1024;
    private final ObjectMapper mapper;
    private final URI endpoint;
    private final String token;
    private final int readTimeoutMs;
    private final HttpClient http;

    public PythonObservabilityClient(ObjectMapper mapper,
            @Value("${platform.observability.python-base-url:http://web:8000}") String baseUrl,
            @Value("${platform.internal-token:}") String token,
            @Value("${platform.observability.connect-timeout-ms:1000}") int connectTimeoutMs,
            @Value("${platform.observability.read-timeout-ms:2000}") int readTimeoutMs,
            @Value("${platform.web-security.environment:development}") String environment,
            @Value("${platform.web-security.public-base-url:http://localhost:8000}") String publicBaseUrl) {
        this.endpoint = endpoint(baseUrl, environment, publicBaseUrl);
        if (connectTimeoutMs < 1 || connectTimeoutMs > 1000 || readTimeoutMs < 1 || readTimeoutMs > 2000) {
            throw new IllegalArgumentException("Invalid observability timeouts");
        }
        if (token.chars().anyMatch(c -> c < 33 || c > 126)
                || ("production".equalsIgnoreCase(environment.strip()) && token.length() < 32)) {
            throw new IllegalArgumentException("Invalid observability service token");
        }
        this.mapper = mapper.rebuild().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
                .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS).build();
        this.token = token;
        this.readTimeoutMs = readTimeoutMs;
        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofMillis(connectTimeoutMs))
                .followRedirects(HttpClient.Redirect.NEVER).proxy(ProxySelector.of(null))
                .version(HttpClient.Version.HTTP_1_1).build();
    }

    public ObjectNode snapshot() {
        if (token.isBlank()) return unavailable();
        CompletableFuture<HttpResponse<byte[]>> pending = null;
        try {
            var request = HttpRequest.newBuilder(endpoint).timeout(Duration.ofMillis(readTimeoutMs))
                    .header("Accept", "application/json").header("X-Internal-Service-Token", token).GET().build();
            pending = http.sendAsync(request, ignored -> new BoundedBody());
            // Bound the entire response, including a stalled body after headers arrive.
            var response = pending.get(readTimeoutMs, TimeUnit.MILLISECONDS);
            if (response.statusCode() != 200 || !response.headers().firstValue("Content-Type").orElse("")
                    .split(";", 2)[0].strip().equalsIgnoreCase("application/json")) return unavailable();
            var result = ObservabilityProjection.runtime(mapper, mapper.readTree(response.body()), true);
            return result.toString().contains(token) ? unavailable() : result;
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            return unavailable();
        } catch (ExecutionException | TimeoutException | tools.jackson.core.JacksonException | IllegalArgumentException invalid) {
            return unavailable();
        } finally {
            if (pending != null && !pending.isDone()) pending.cancel(true);
        }
    }

    private ObjectNode unavailable() {
        var result = ObservabilityProjection.unavailable(mapper);
        result.set("account_email", ObservabilityProjection.unavailableEmail(mapper));
        return result;
    }

    private static final class BoundedBody implements HttpResponse.BodySubscriber<byte[]> {
        private final HttpResponse.BodySubscriber<byte[]> delegate = HttpResponse.BodySubscribers.ofByteArray();
        private Flow.Subscription subscription;
        private long received;
        private boolean failed;
        @Override public CompletionStage<byte[]> getBody() { return delegate.getBody(); }
        @Override public void onSubscribe(Flow.Subscription subscription) {
            this.subscription = subscription;
            delegate.onSubscribe(subscription);
        }
        @Override public void onNext(List<ByteBuffer> buffers) {
            if (failed) return;
            for (ByteBuffer buffer : buffers) received += buffer.remaining();
            if (received > MAX_RESPONSE_BYTES) {
                failed = true;
                subscription.cancel();
                delegate.onError(new IOException("Observability response is too large"));
            } else delegate.onNext(buffers);
        }
        @Override public void onError(Throwable error) { if (!failed) delegate.onError(error); }
        @Override public void onComplete() { if (!failed) delegate.onComplete(); }
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
            return base.resolve("/internal/v1/observability/requests");
        } catch (IllegalArgumentException invalid) {
            throw new IllegalArgumentException("Observability base URL must be a service origin; production requires a private internal host");
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
