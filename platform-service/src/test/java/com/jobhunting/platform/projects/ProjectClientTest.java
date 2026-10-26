package com.jobhunting.platform.projects;

import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicInteger;
import com.jobhunting.platform.auth.AuthException;
import org.junit.jupiter.api.*;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;
import static org.assertj.core.api.Assertions.*;

class ProjectClientTest {
    final ObjectMapper mapper=new ObjectMapper();
    final AtomicInteger calls=new AtomicInteger();
    HttpServer server;ProjectClient client;
    String body="{\"account_id\":42,\"project_cards\":[]}";int status=200;String mime="application/json";
    @BeforeEach void setup()throws Exception{
        server=HttpServer.create(new InetSocketAddress("127.0.0.1",0),0);
        server.createContext("/internal/v1/projects/",exchange->{
            calls.incrementAndGet();var data=mapper.readTree(exchange.getRequestBody().readAllBytes());
            assertThat(data.get("session_token").asText()).isEqualTo("real-session");
            assertThat(data.has("account_id")).isFalse();
            assertThat(exchange.getRequestHeaders().getFirst("X-Internal-Service-Token")).isEqualTo("secret");
            byte[] bytes=body.getBytes(StandardCharsets.UTF_8);exchange.getResponseHeaders().set("Content-Type",mime);
            exchange.sendResponseHeaders(status,bytes.length);exchange.getResponseBody().write(bytes);exchange.close();
        });server.start();
        client=new ProjectClient(mapper,"http://127.0.0.1:"+server.getAddress().getPort(),"secret","development","http://localhost:8000");
    }
    @AfterEach void close(){server.stop(0);}
    ObjectNode input(){return mapper.createObjectNode().put("candidate_id",7L);}
    @Test void validatesScopeAndRemovesPrivateOwner(){
        assertThat(client.execute("list","real-session",42,input(),"trace").toString()).isEqualTo("{\"project_cards\":[]}");
        body="{\"account_id\":42,\"deleted\":true,\"project_card_id\":9,\"candidate_id\":7,\"cleanup_status\":\"pending\",\"rag_cleanup\":\"database_cascade\"}";
        assertThat(client.execute("delete","real-session",42,mapper.createObjectNode().put("record_id",9L),"trace").path("deleted").asBoolean()).isTrue();
    }
    @Test void malformedForeignExtraOversizedAndWrongContentNeverSucceed(){
        for(String invalid:new String[]{"{}","{\"account_id\":99,\"project_cards\":[]}","{\"account_id\":42,\"project_cards\":[],\"private\":true}",
            "{\"account_id\":42,\"project_cards\":[],\"project_cards\":[]}","{\"account_id\":42,\"project_cards\":\""+"x".repeat(ProjectClient.MAX_BYTES)+"\"}"}){
            body=invalid;assertThatThrownBy(()->client.execute("list","real-session",42,input(),"trace")).isInstanceOf(AuthException.class);
        }
        body="{\"account_id\":42,\"project_cards\":[]}";mime="text/plain";
        assertThatThrownBy(()->client.execute("list","real-session",42,input(),"trace")).isInstanceOf(AuthException.class);
    }
    @Test void errorsAreRedactedAndNeverReplayed(){
        status=409;body="{\"code\":\"PROJECT_BUSY\",\"detail\":\"private provider credential\"}";
        assertThatThrownBy(()->client.execute("delete","real-session",42,mapper.createObjectNode().put("record_id",9),"trace"))
            .isInstanceOf(AuthException.class).hasMessageNotContaining("private");assertThat(calls.get()).isEqualTo(1);
        status=500;assertThatThrownBy(()->client.execute("list","real-session",42,input(),"trace")).hasMessageNotContaining("private");
    }
    @Test void missingDevelopmentTokenFailsClosedAndProductionRequiresInternalOrigin(){
        var isolated=new ProjectClient(mapper,"http://web:8000","","development","http://localhost:8000");
        assertThatThrownBy(()->isolated.execute("list","real-session",42,input(),"trace")).isInstanceOf(AuthException.class);
        assertThat(calls.get()).isZero();
        for(String url:new String[]{"https://public.example.com","http://web:8000/path","http://secret@web:8000","http://web:0"})
            assertThatThrownBy(()->new ProjectClient(mapper,url,"x".repeat(32),"production","https://public.example.com")).isInstanceOf(IllegalArgumentException.class);
    }
    @Test void githubProjectionBindsOwnerCandidateAndNeverLeaksTaskResult(){
        var task=mapper.createObjectNode().put("task_key","abc").put("task_type","github_project_analysis").put("status","queued")
            .put("progress",0).put("attempt",0).put("max_attempts",3).putNull("result").putNull("error_summary")
            .put("created_at","now").put("updated_at","now").putNull("started_at").putNull("finished_at");
        var projection=mapper.createObjectNode().put("account_id",42).put("submission_candidate_id",7).putNull("project_card")
            .put("processing_async",true).put("root_request_id","a".repeat(32));projection.set("task",task);
        body=projection.toString();
        var result=client.execute("github","real-session",42,input(),"trace");
        assertThat(result.has("account_id")).isFalse();assertThat(result.has("submission_candidate_id")).isFalse();
        projection.put("submission_candidate_id",8);body=projection.toString();
        assertThatThrownBy(()->client.execute("github","real-session",42,input(),"trace")).isInstanceOf(AuthException.class);
        projection.put("submission_candidate_id",7);task.put("result","private source");body=projection.toString();
        assertThatThrownBy(()->client.execute("github","real-session",42,input(),"trace")).isInstanceOf(AuthException.class);
    }
    ObjectNode collectionBody(int count){
        var c=mapper.createObjectNode().put("id",8).put("candidate_id",7).putNull("project_card_id").put("project_name","Design")
            .put("source_type","local_directory_collection").put("manifest_fingerprint","a".repeat(64)).put("status","planned")
            .put("file_count",count).put("selected_file_count",1).put("uploaded_file_count",0).put("total_size",count).put("selected_size",1)
            .putNull("error_summary").put("created_at","now").put("updated_at","now");
        var files=mapper.createArrayNode();
        for(int i=1;i<=count;i++){
            var f=mapper.createObjectNode().put("id",i).put("collection_id",8).put("relative_path",i+"-"+"p".repeat(100)+".txt")
                .put("file_kind","source_text").put("media_type","text/plain").put("file_size",1).put("client_sha256","a".repeat(64))
                .putNull("server_sha256").put("selection_status",i==1?"selected":"skipped").put("selection_reason","policy")
                .putNull("extraction_method").put("text_length",0).putNull("long_text_id").put("created_at","now").put("updated_at","now");
            f.set("metadata",mapper.createObjectNode().putNull("last_modified"));files.add(f);
        }
        var root=mapper.createObjectNode().put("account_id",42);root.set("collection",c);root.set("files",files);return root;
    }
    @Test void localViewsRejectWrongScopePrivateFieldsAndInvalidPlans(){
        var data=collectionBody(1);body=data.toString();var query=mapper.createObjectNode().put("collection_id",8);
        assertThat(client.execute("local-get","real-session",42,query,"trace").has("account_id")).isFalse();
        ((ObjectNode)data.get("files").get(0).get("metadata")).put("storage_key","private");body=data.toString();
        assertThatThrownBy(()->client.execute("local-get","real-session",42,query,"trace")).isInstanceOf(AuthException.class);
        data=collectionBody(1);((ObjectNode)data.get("collection")).put("id",9);body=data.toString();
        assertThatThrownBy(()->client.execute("local-get","real-session",42,query,"trace")).isInstanceOf(AuthException.class);
        data=collectionBody(1);data.set("selected_files",mapper.createArrayNode());data.put("resumed",false);body=data.toString();
        assertThatThrownBy(()->client.execute("local-manifest","real-session",42,input(),"trace")).isInstanceOf(AuthException.class);
        data.set("selected_files",data.get("files").deepCopy());body=data.toString();
        assertThat(client.execute("local-manifest","real-session",42,input(),"trace").path("resumed").asBoolean()).isFalse();
    }
    @Test void boundedLargeCollectionUsesSeparateLimitAndOversizedManifestNeverLeavesJava(){
        body=collectionBody(10000).toString();assertThat(body.length()).isGreaterThan(ProjectClient.MAX_BYTES);
        assertThat(client.execute("local-get","real-session",42,mapper.createObjectNode().put("collection_id",8),"trace").path("files").size()).isEqualTo(10000);
        int before=calls.get();
        assertThatThrownBy(()->client.execute("local-manifest","real-session",42,input().put("project_name","x".repeat(ProjectClient.MAX_BYTES)),"trace"))
            .isInstanceOf(AuthException.class);
        assertThat(calls.get()).isEqualTo(before);
    }
}
