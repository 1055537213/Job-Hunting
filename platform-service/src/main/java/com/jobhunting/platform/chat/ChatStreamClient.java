package com.jobhunting.platform.chat;

import java.io.IOException;
import java.net.ProxySelector;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Set;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.Semaphore;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Flow;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Function;
import com.jobhunting.platform.auth.AuthException;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.core.ResolvableType;
import org.springframework.core.io.buffer.DefaultDataBufferFactory;
import org.springframework.core.io.buffer.DataBuffer;
import org.springframework.core.io.buffer.DataBufferUtils;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ReactiveHttpInputMessage;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.http.codec.ServerSentEventHttpMessageReader;
import org.springframework.stereotype.Component;
import reactor.adapter.JdkFlowAdapter;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import tools.jackson.core.StreamReadFeature;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

@Component
@ConditionalOnProperty(name="platform.auth.enabled", havingValue="true")
public class ChatStreamClient {
    private static final ResolvableType EVENTS = ResolvableType.forClassWithGenerics(ServerSentEvent.class,String.class);
    private static final int MAX_STREAM_BYTES = 8 * 1024 * 1024;
    private final HttpClient http;
    private final java.net.URI endpoint;
    private final ServerSentEventHttpMessageReader reader = new ServerSentEventHttpMessageReader();
    private final ObjectMapper mapper;
    private final String token;
    private final Duration deadline;
    private final Semaphore slots = new Semaphore(8);

    public ChatStreamClient(ObjectMapper mapper,
            @Value("${platform.chat.python-base-url:http://web:8000}") String baseUrl,
            @Value("${platform.internal-token:}") String token,
            @Value("${platform.web-security.environment:development}") String environment,
            @Value("${platform.web-security.public-base-url:http://localhost:8000}") String publicUrl,
            @Value("${platform.chat.invoke-timeout-ms:300000}") int timeoutMs) {
        endpoint = ChatSessionClient.endpoint(baseUrl, environment, publicUrl).resolve("../stream");
        if (token.isBlank() || token.chars().anyMatch(c -> c < 33 || c > 126) || timeoutMs < 1 || timeoutMs > 300000
                || ("production".equalsIgnoreCase(environment.strip()) && token.length() < 32))
            throw new IllegalArgumentException("Invalid stream callback settings");
        this.token = token; deadline = Duration.ofMillis(timeoutMs);
        this.mapper = mapper.rebuild().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
            .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS).build();
        http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(1)).proxy(ProxySelector.of(null))
            .followRedirects(HttpClient.Redirect.NEVER).version(HttpClient.Version.HTTP_1_1).build();
        reader.setMaxInMemorySize(ChatSessionClient.MAX_BYTES);
    }

    public void relay(String session, long owner, ObjectNode data, String requestId, HttpServletResponse response) {
        if (!slots.tryAcquire()) throw ChatSessionClient.unconfirmed();
        boolean started = false;
        CompletableFuture<HttpResponse<Flow.Publisher<List<ByteBuffer>>>> pending = null;
        var subscription = new AtomicReference<org.reactivestreams.Subscription>();
        try {
            String execution = UUID.randomUUID().toString().replace("-", "");
            String key = data.path("session_id").isTextual() ? data.get("session_id").asText()
                : "account-"+owner+"-candidate-"+data.get("candidate_id").asLong();
            byte[] payload = mapper.writeValueAsBytes(data.deepCopy().put("session_token", session).put("execution_id", execution));
            var request=HttpRequest.newBuilder(endpoint).timeout(deadline)
                .header("Content-Type","application/json").header("Accept","text/event-stream")
                .header("X-Internal-Service-Token",token).header("X-Request-ID",requestId).header("X-Trace-Id",requestId)
                .POST(ChatSessionClient.singleUse(payload)).build();
            pending=http.sendAsync(request,HttpResponse.BodyHandlers.ofPublisher());
            var events=Mono.fromFuture(pending).flatMapMany(upstream -> decode(upstream,subscription))
                .timeout(Duration.ofSeconds(15)).take(deadline);
            // Closing the bounded stream cancels its subscription and the upstream HTTP body.
            try (var stream = events.toStream(1)) {
                var iterator = stream.iterator();
                if (!iterator.hasNext()) throw ChatSessionClient.unconfirmed();
                var first = iterator.next();
                var scope = json(first);
                if (!"scope".equals(first.event()) || !ChatSessionClient.fields(scope, Set.of("account_id", "candidate_id", "session_id", "execution_id"))
                        || !ChatSessionClient.id(scope.get("account_id")) || scope.get("account_id").asLong() != owner
                        || !data.get("candidate_id").equals(scope.get("candidate_id"))
                        || !scope.path("session_id").isTextual() || !key.equals(scope.get("session_id").asText())
                        || !scope.path("execution_id").isTextual() || !execution.equals(scope.get("execution_id").asText())) throw ChatSessionClient.unconfirmed();
                response.setContentType("text/event-stream;charset=UTF-8");
                response.setHeader("Cache-Control", "no-store"); response.setHeader("X-Accel-Buffering", "no");
                started = true;
                write(response, ": connected\n\n");
                long total = 0;
                while (iterator.hasNext()) {
                    var event = iterator.next();
                    if (event.data() == null && event.comment() != null) { write(response, ": keepalive\n\n"); continue; }
                    String name = event.event();
                    var value = json(event);
                    validateEvent(name, value, data.get("candidate_id").asLong(), execution);
                    String frame = "event: "+name+"\ndata: "+mapper.writeValueAsString(value)+"\n\n";
                    total += frame.getBytes(StandardCharsets.UTF_8).length;
                    if (total > MAX_STREAM_BYTES) throw ChatSessionClient.unconfirmed();
                    write(response, frame);
                    if ("final".equals(name) || "error".equals(name)) return;
                }
                throw ChatSessionClient.unconfirmed();
            }
        } catch (IOException error) {
            // A disconnected browser must cancel upstream, not trigger another chat command.
        } catch (Exception error) {
            if (!started) {
                if (error instanceof AuthException auth) throw auth;
                throw ChatSessionClient.unconfirmed();
            }
            try { write(response, "event: error\ndata: {\"code\":\"CHAT_EXECUTION_UNCONFIRMED\",\"detail\":\"对话结果暂未确认，请先查看历史记录，勿立即重复发送。\"}\n\n"); }
            catch (IOException ignored) { }
        } finally {
            var active=subscription.get(); if(active!=null)active.cancel();
            if(pending!=null&&!pending.isDone())pending.cancel(true);
            slots.release();
        }
    }

    private Flux<ServerSentEvent<String>> decode(HttpResponse<Flow.Publisher<List<ByteBuffer>>> upstream,
            AtomicReference<org.reactivestreams.Subscription> subscription) {
        var bytes=new AtomicLong();
        Flux<DataBuffer> body=JdkFlowAdapter.flowPublisherToFlux(upstream.body()).doOnSubscribe(subscription::set)
            .concatMapIterable(Function.identity(),1).map(buffer -> {
                if(bytes.addAndGet(buffer.remaining())>MAX_STREAM_BYTES)throw ChatSessionClient.unconfirmed();
                return (DataBuffer)DefaultDataBufferFactory.sharedInstance.wrap(buffer);
            }).doOnDiscard(DataBuffer.class,DataBufferUtils::release);
        if(upstream.statusCode()!=200) {
            return DataBufferUtils.join(body,65536).flatMapMany(buffer -> {
                try {
                    var value=mapper.readTree(buffer.toString(StandardCharsets.UTF_8));
                    if(!ChatSessionClient.fields(value,Set.of("code","detail"))||!value.path("code").isTextual())
                        return Flux.error(ChatSessionClient.unconfirmed());
                    return Flux.error(ChatSessionClient.invocationError(upstream.statusCode(),value.get("code").asText()));
                } finally { DataBufferUtils.release(buffer); }
            });
        }
        if(!upstream.headers().firstValue("Content-Type").orElse("").split(";",2)[0].strip().equalsIgnoreCase("text/event-stream"))
            return body.take(1).thenMany(Flux.error(ChatSessionClient.unconfirmed()));
        var message=new ReactiveHttpInputMessage() {
            @Override public HttpHeaders getHeaders() { var h=new HttpHeaders();h.setContentType(MediaType.TEXT_EVENT_STREAM);return h; }
            @Override public Flux<DataBuffer> getBody() { return body; }
        };
        return reader.read(EVENTS,message,Map.of()).map(value -> {
            if(!(value instanceof ServerSentEvent<?> event) || (event.data()!=null && !(event.data() instanceof String)))
                throw ChatSessionClient.unconfirmed();
            return ServerSentEvent.<String>builder().event(event.event()).comment(event.comment()).data((String)event.data()).build();
        });
    }

    private JsonNode json(ServerSentEvent<String> event) {
        if (event.data() == null || event.data().getBytes(StandardCharsets.UTF_8).length > ChatSessionClient.MAX_BYTES)
            throw ChatSessionClient.unconfirmed();
        var value = mapper.readTree(event.data());
        if (value == null || !value.isObject()) throw ChatSessionClient.unconfirmed();
        return value;
    }
    static void validateEvent(String name, JsonNode value, long candidate, String execution) {
        if (name == null) throw ChatSessionClient.unconfirmed();
        switch (name) {
            case "token", "status" -> {
                if (!ChatSessionClient.fields(value, Set.of("content")) || !value.get("content").isTextual()) throw ChatSessionClient.unconfirmed();
            }
            case "task_started", "task_completed", "task_failed", "approval_required" -> {
                if (!ChatSessionClient.fields(value, Set.of("task_trace")) || value.get("task_trace").isNull()) throw ChatSessionClient.unconfirmed();
                ChatReply.validateTrace(value.get("task_trace"), execution);
            }
            case "step_started", "step_completed" -> {
                if (!ChatSessionClient.fields(value, Set.of("step"))) throw ChatSessionClient.unconfirmed();
                ChatReply.validateStep(value.get("step"));
            }
            case "final" -> ChatReply.validate(value, candidate, execution);
            case "error" -> {
                String message = value.path("detail").asText();
                ((ObjectNode)value).removeAll();
                ((ObjectNode)value).put("code", "余额不足，请先充值后重试。".equals(message) ? "INSUFFICIENT_BALANCE" : "CHAT_EXECUTION_UNCONFIRMED")
                    .put("detail", "余额不足，请先充值后重试。".equals(message) ? message : "对话结果暂未确认，请先查看历史记录，勿立即重复发送。");
            }
            default -> throw ChatSessionClient.unconfirmed();
        }
    }
    private static void write(HttpServletResponse response, String frame) throws IOException {
        response.getOutputStream().write(frame.getBytes(StandardCharsets.UTF_8));
        response.flushBuffer();
    }
}
