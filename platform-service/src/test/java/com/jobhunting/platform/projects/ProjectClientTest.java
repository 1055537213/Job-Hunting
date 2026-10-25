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
}
