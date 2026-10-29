package com.jobhunting.platform.projects;

import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.net.URI;
import java.net.ProxySelector;
import java.net.http.*;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
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
import org.springframework.web.multipart.MultipartFile;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import tools.jackson.core.StreamReadFeature;
import tools.jackson.databind.*;
import tools.jackson.databind.node.ObjectNode;

@Component
@ConditionalOnProperty(name="platform.auth.enabled",havingValue="true")
public class ProjectClient {
    private static final Logger LOG=LoggerFactory.getLogger(ProjectClient.class);
    static final int MAX_BYTES=4*1024*1024;
    static final long MAX_UPLOAD_FILE_BYTES=50L*1024*1024;
    static final long MAX_UPLOAD_BATCH_BYTES=160L*1024*1024;
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
        catch(Exception error){LOG.warn("Project callback failed operation={} error_type={}",operation,error.getClass().getSimpleName());throw unavailable();}
        finally{if(pending!=null&&!pending.isDone())pending.cancel(true);slots.release();if(local)collectionSlots.release();}
    }

    public JsonNode executeMultipart(String session,long owner,long collectionId,List<Long> fileIds,
            List<MultipartFile> files,String requestId) {
        if (token.isBlank() || fileIds==null || files==null || fileIds.size()<1 || fileIds.size()>5 || fileIds.size()!=files.size()) throw invalid();
        long total=0;
        for (int i=0;i<fileIds.size();i++) {
            if (!id(mapper.valueToTree(fileIds.get(i)))) throw invalid();
            long size=files.get(i).getSize();
            if (size<0 || size>MAX_UPLOAD_FILE_BYTES) throw new AuthException("PROJECT_UPLOAD_TOO_LARGE","项目文件批次超过上传限制。",HttpStatus.PAYLOAD_TOO_LARGE);
            total=Math.addExact(total,size);
        }
        if (total>MAX_UPLOAD_BATCH_BYTES) throw new AuthException("PROJECT_UPLOAD_TOO_LARGE","项目文件批次超过上传限制。",HttpStatus.PAYLOAD_TOO_LARGE);
        if(!collectionSlots.tryAcquire())throw unavailable();
        if(!slots.tryAcquire()){collectionSlots.release();throw unavailable();}
        CompletableFuture<HttpResponse<byte[]>> pending=null;
        try {
            String boundary="jobagent-"+UUID.randomUUID().toString().replace("-","");
            String ids=mapper.writeValueAsString(fileIds);
            var body=MultipartBody.publisher(boundary,session,ids,fileIds,files);
            var request=TraceHeaders.apply(HttpRequest.newBuilder(base.resolve("local/"+collectionId+"/files")),requestId)
                .timeout(Duration.ofSeconds(300)).header("Content-Type","multipart/form-data; boundary="+boundary)
                .header("Accept","application/json").header("X-Internal-Service-Token",token).POST(body).build();
            pending=http.sendAsync(request,ignored->new BoundedBody(4*1024*1024));
            var response=pending.get(300,TimeUnit.SECONDS);
            if(!response.headers().firstValue("Content-Type").orElse("").split(";",2)[0].strip().equalsIgnoreCase("application/json"))throw unavailable();
            var value=mapper.readTree(response.body());
            if(response.statusCode()!=200){
                if(!fields(value,Set.of("code","detail"))||!value.path("code").isTextual()||!value.path("detail").isTextual())throw unavailable();
                throw safeError(response.statusCode(),value.get("code").asText());
            }
            if(value==null||!value.isObject()||!id(value.get("account_id"))||value.get("account_id").asLong()!=owner)throw unavailable();
            var result=(ObjectNode)value.deepCopy();result.remove("account_id");
            validateUploadProjection(result);
            return result;
        }catch(AuthException error){LOG.warn("Project multipart callback rejected code={}",error.code());throw error;}
        catch(InterruptedException error){Thread.currentThread().interrupt();throw unavailable();}
        catch(Exception error){LOG.warn("Project multipart callback failed type={}",error.getClass().getSimpleName());throw unavailable();}
        finally{if(pending!=null&&!pending.isDone())pending.cancel(true);slots.release();collectionSlots.release();}
    }

    private static void validateUploadProjection(JsonNode value){
        require(fields(value,Set.of("processed_files","failed_files","rag_task","visual_task","visual_index","visual_warning")));
        require(value.get("processed_files").isArray()&&value.get("failed_files").isArray());
        for(var file:value.get("processed_files")){
            require(fields(file,Set.of("id","collection_id","relative_path","file_size","selection_status","selection_reason","extraction_method","text_length","created_at","updated_at"))
                &&id(file.get("id"))&&id(file.get("collection_id"))&&file.get("relative_path").isTextual()
                &&file.get("file_size").isIntegralNumber()&&file.get("file_size").asLong()>=0&&file.get("text_length").isIntegralNumber()
                &&file.get("text_length").asLong()>=0&&file.get("selection_status").isTextual()&&file.get("selection_reason").isTextual()
                &&(file.get("extraction_method").isNull()||file.get("extraction_method").isTextual())&&file.get("created_at").isTextual()&&file.get("updated_at").isTextual());
        }
        for(var failed:value.get("failed_files"))require(fields(failed,Set.of("id","selection_reason"))&&(failed.get("id").isIntegralNumber()||failed.get("id").isTextual())&&failed.get("selection_reason").isTextual());
        for(var key:Set.of("rag_task","visual_task")){
            var task=value.get(key);
            if(task.isNull())continue;
            require(fields(task,Set.of("task_key","task_type","status","progress","attempt","max_attempts","result","error_summary","created_at","started_at","finished_at","updated_at"))
                &&task.get("task_key").isTextual()&&task.get("task_key").asText().matches("[A-Za-z0-9_-]{1,64}")
                &&task.get("task_type").isTextual()&&Set.of("rag_index","visual_index").contains(task.get("task_type").asText())
                &&task.get("status").isTextual()&&Set.of("queued","running","succeeded","failed","cancelled").contains(task.get("status").asText())
                &&task.get("progress").isIntegralNumber()&&task.get("progress").asInt()>=0&&task.get("progress").asInt()<=100
                &&task.get("attempt").isIntegralNumber()&&task.get("attempt").asLong()>=0&&id(task.get("max_attempts"))
                &&(task.get("result").isNull()||(task.get("result").isObject()&&task.get("result").size()==0))
                &&task.get("created_at").isTextual()&&task.get("updated_at").isTextual());
        }
        var visual=value.get("visual_index");
        require(visual.isNull()||(fields(visual,Set.of("indexed"))&&visual.get("indexed").isBoolean()));
        require(value.get("visual_warning").isNull()||value.get("visual_warning").isTextual()&&value.get("visual_warning").asText().length()<=512);
    }

    private static final class MultipartBody {
        private record Part(byte[] prefix, MultipartFile file, byte[] suffix){}
        static HttpRequest.BodyPublisher publisher(String boundary,String session,String fileIds,List<Long> ids,List<MultipartFile> files){
            byte[] field=("--"+boundary+"\r\nContent-Disposition: form-data; name=\"session_token\"\r\n\r\n"+session+"\r\n"
                +"--"+boundary+"\r\nContent-Disposition: form-data; name=\"file_ids\"\r\nContent-Type: application/json\r\n\r\n"+fileIds+"\r\n").getBytes(StandardCharsets.UTF_8);
            List<Part> parts=new ArrayList<>();long length=field.length;
            for(int i=0;i<files.size();i++){
                String header="--"+boundary+"\r\nContent-Disposition: form-data; name=\"files\"; filename=\"file-"+ids.get(i)+"\"\r\nContent-Type: application/octet-stream\r\n\r\n";
                byte[] prefix=header.getBytes(StandardCharsets.UTF_8),suffix="\r\n".getBytes(StandardCharsets.UTF_8);parts.add(new Part(prefix,files.get(i),suffix));length+=prefix.length+files.get(i).getSize()+suffix.length;
            }
            byte[] end=("--"+boundary+"--\r\n").getBytes(StandardCharsets.UTF_8);length+=end.length;
            final long totalLength=length;
            return new HttpRequest.BodyPublisher(){
                public long contentLength(){return totalLength;}
                public void subscribe(Flow.Subscriber<? super ByteBuffer> subscriber){
                    List<InputStream> streams=new ArrayList<>();streams.add(new java.io.ByteArrayInputStream(field));
                    try{for(var part:parts){streams.add(new java.io.ByteArrayInputStream(part.prefix()));streams.add(part.file().getInputStream());streams.add(new java.io.ByteArrayInputStream(part.suffix()));}streams.add(new java.io.ByteArrayInputStream(end));
                        HttpRequest.BodyPublishers.ofInputStream(()->new java.io.SequenceInputStream(Collections.enumeration(streams))).subscribe(subscriber);
                    }catch(IOException error){subscriber.onSubscribe(new Flow.Subscription(){public void request(long n){}public void cancel(){}});subscriber.onError(new UncheckedIOException(error));}
                }
            };
        }
    }
    static boolean id(JsonNode n){return n!=null&&n.isIntegralNumber()&&n.canConvertToLong()&&n.asLong()>0;}
    private static void require(boolean condition){if(!condition)throw unavailable();}
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
            case "400:PROJECT_UPLOAD_INVALID"->"项目文件批次无效，请刷新采集进度后重试。";
            case "413:PROJECT_UPLOAD_TOO_LARGE"->"项目文件批次超过上传限制。";
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
