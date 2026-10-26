package com.jobhunting.platform.projects;

import java.io.IOException;
import java.net.URI;
import java.net.ProxySelector;
import java.net.http.*;
import java.nio.ByteBuffer;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import com.jobhunting.platform.auth.AuthException;
import com.jobhunting.platform.observability.TraceHeaders;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import tools.jackson.core.StreamReadFeature;
import tools.jackson.databind.*;
import tools.jackson.databind.node.ObjectNode;

@Component
@ConditionalOnProperty(name="platform.auth.enabled",havingValue="true")
public class ProjectClient {
    static final int MAX_BYTES=4*1024*1024;
    private final URI base;
    private final String token;
    private final ObjectMapper mapper;
    private final HttpClient http;
    private final Semaphore slots=new Semaphore(8);
    private final Semaphore collectionSlots=new Semaphore(2);
    public ProjectClient(ObjectMapper mapper,
            @Value("${platform.projects.python-base-url:http://web:8000}") String url,
            @Value("${platform.internal-token:}") String token,
            @Value("${platform.web-security.environment:development}") String environment,
            @Value("${platform.web-security.public-base-url:http://localhost:8000}") String publicUrl) {
        var origin=URI.create(url);
        if (!("http".equals(origin.getScheme()) || "https".equals(origin.getScheme())) || origin.getHost()==null || origin.getUserInfo()!=null
                || origin.getQuery()!=null || origin.getFragment()!=null || !Set.of("","/").contains(origin.getRawPath())
                || origin.getPort()==0 || origin.getPort()>65535 || token.chars().anyMatch(c->c<33 || c>126)
                || ("production".equalsIgnoreCase(environment.strip()) && (token.length()<32
                    || !Set.of("web","localhost","127.0.0.1","[::1]").contains(origin.getHost())
                    || origin.getHost().equalsIgnoreCase(URI.create(publicUrl).getHost())))) throw new IllegalArgumentException("Invalid project callback origin or credentials");
        base=origin.resolve("/internal/v1/projects/"); this.token=token;
        this.mapper=mapper.rebuild().enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION).enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS).build();
        http=HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(1)).proxy(ProxySelector.of(null))
            .followRedirects(HttpClient.Redirect.NEVER).version(HttpClient.Version.HTTP_1_1).build();
    }
    public JsonNode execute(String operation,String session,long owner,ObjectNode input,String requestId) {
        if (!Set.of("list","confirm","delete","github","local-manifest","local-get","local-cancel","local-complete").contains(operation)) throw invalid();
        boolean local=operation.startsWith("local-");
        if (token.isBlank()) throw unavailable();
        if(local&&!collectionSlots.tryAcquire())throw unavailable();
        if(!slots.tryAcquire()){if(local)collectionSlots.release();throw unavailable();}
        CompletableFuture<HttpResponse<byte[]>> pending=null;
        try {
            var bytes=mapper.writeValueAsBytes(input.deepCopy().put("session_token",session));
            if(bytes.length>MAX_BYTES)throw new AuthException("PROJECT_TOO_LARGE","项目清单超过请求大小限制。",HttpStatus.PAYLOAD_TOO_LARGE);
            var body=HttpRequest.BodyPublishers.ofByteArray(bytes);
            var used=new AtomicBoolean();
            // JDK may retry a failed connection: never replay a state-changing callback body.
            var once=new HttpRequest.BodyPublisher() {
                public long contentLength(){return body.contentLength();}
                public void subscribe(Flow.Subscriber<? super ByteBuffer> s){
                    if(used.compareAndSet(false,true))body.subscribe(s);
                    else {s.onSubscribe(new Flow.Subscription(){public void request(long n){} public void cancel(){}});s.onError(new IOException("Replay disabled"));}
                }
            };
            var request=TraceHeaders.apply(HttpRequest.newBuilder(base.resolve(operation)),requestId).timeout(Duration.ofSeconds(10))
                .header("Content-Type","application/json").header("Accept","application/json").header("X-Internal-Service-Token",token).POST(once).build();
            pending=http.sendAsync(request,ignored->new BoundedBody(Set.of("local-manifest","local-get","local-cancel").contains(operation)?LocalCollectionProjection.MAX_BYTES:MAX_BYTES));
            var response=pending.get(10,TimeUnit.SECONDS);
            if(!response.headers().firstValue("Content-Type").orElse("").split(";",2)[0].strip().equalsIgnoreCase("application/json"))throw unavailable();
            var value=mapper.readTree(response.body());
            if(response.statusCode()!=200){
                if(!fields(value,Set.of("code","detail")) || !value.path("code").isTextual() || !value.path("detail").isTextual())throw unavailable();
                throw safeError(response.statusCode(),value.get("code").asText());
            }
            if(value==null || !value.isObject() || !id(value.get("account_id")) || value.get("account_id").asLong()!=owner)throw unavailable();
            var result=(ObjectNode)value.deepCopy();result.remove("account_id");
            if("github".equals(operation)) {
                if(!id(result.get("submission_candidate_id")) || result.get("submission_candidate_id").asLong()!=input.path("candidate_id").asLong())throw unavailable();
                result.remove("submission_candidate_id");
            }
            if("local-complete".equals(operation)){
                if(!id(result.get("completion_collection_id")) || result.get("completion_collection_id").asLong()!=input.path("collection_id").asLong())throw unavailable();
                result.remove("completion_collection_id");
            }
            ProjectProjection.validate(result,operation,input);
            return result;
        }catch(AuthException error){throw error;}
        catch(InterruptedException error){Thread.currentThread().interrupt();throw unavailable();}
        catch(Exception error){throw unavailable();}
        finally{if(pending!=null&&!pending.isDone())pending.cancel(true);slots.release();if(local)collectionSlots.release();}
    }
    static boolean id(JsonNode n){return n!=null&&n.isIntegralNumber()&&n.canConvertToLong()&&n.asLong()>0;}
    static boolean fields(JsonNode n,Set<String> keys){return n!=null&&n.isObject()&&n.propertyNames().equals(keys);}
    static AuthException unavailable(){return new AuthException("PROJECT_UNAVAILABLE","项目操作结果暂未确认，请刷新项目列表核实后重试。",HttpStatus.SERVICE_UNAVAILABLE);}
    static AuthException invalid(){return new AuthException("INVALID_REQUEST","请求参数无效。",HttpStatus.UNPROCESSABLE_CONTENT);}
    private static AuthException safeError(int status,String code){
        String message=switch(status+":"+code){
            case "401:SESSION_EXPIRED"->"登录状态已过期，请重新登录。";
            case "403:PROJECT_SCOPE_DENIED"->"账号不可用或无权访问项目。";
            case "404:PROJECT_NOT_FOUND"->"项目或档案不存在。";
            case "400:PROJECT_CONFIRMATION_REQUIRED"->"确认项目经历前至少确认一组属于本人的内容。";
            case "400:PROJECT_REPOSITORY_INVALID"->"请填写有效的公开 GitHub 仓库首页链接。";
            case "409:PROJECT_SUBMISSION_CONFLICT"->"本次提交已失效或内容已变更，请重新发起项目分析。";
            case "400:PROJECT_MANIFEST_INVALID"->"项目清单无效或没有可安全分析的文件，请重新选择目录。";
            case "409:PROJECT_COLLECTION_CONFLICT"->"采集已完成或状态已变化，请刷新后核实；已生成的项目请通过项目列表删除。";
            case "409:PROJECT_COLLECTION_INCOMPLETE"->"项目文件尚未全部处理，或没有成功提取的内容，请检查采集进度后重试。";
            case "409:PROJECT_BUSY"->"当前仍有请求或任务执行，请稍后重试。";
            case "409:PROJECT_CONFLICT"->"已确认内容与本次请求不一致，请刷新项目列表核实。";
            case "413:PROJECT_TOO_LARGE"->"项目数据超出同步查询限制。";
            case "422:INVALID_REQUEST"->"请求参数无效。";
            default->null;
        };
        return message==null?unavailable():new AuthException(code,message,HttpStatus.valueOf(status));
    }
    private static final class BoundedBody implements HttpResponse.BodySubscriber<byte[]>{
        private final HttpResponse.BodySubscriber<byte[]> delegate=HttpResponse.BodySubscribers.ofByteArray();
        private Flow.Subscription subscription;private long size;private boolean failed;
        private final int limit;
        BoundedBody(int limit){this.limit=limit;}
        public CompletionStage<byte[]> getBody(){return delegate.getBody();}
        public void onSubscribe(Flow.Subscription s){subscription=s;delegate.onSubscribe(s);}
        public void onNext(List<ByteBuffer> buffers){if(failed)return;for(var b:buffers)size+=b.remaining();
            if(size>limit){failed=true;subscription.cancel();delegate.onError(new IOException("Project response too large"));}else delegate.onNext(buffers);}
        public void onError(Throwable e){if(!failed)delegate.onError(e);}
        public void onComplete(){if(!failed)delegate.onComplete();}
    }
}
