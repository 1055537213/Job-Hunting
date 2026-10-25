package com.jobhunting.platform.chat;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import com.sun.net.httpserver.HttpServer;
import com.jobhunting.platform.auth.AuthException;
import org.junit.jupiter.api.*;
import org.springframework.mock.web.MockHttpServletResponse;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;
import static org.assertj.core.api.Assertions.*;

class ChatStreamClientTest {
    final ObjectMapper mapper = new ObjectMapper();
    final AtomicInteger calls = new AtomicInteger();
    final CountDownLatch tokenSent = new CountDownLatch(1), allowFinal = new CountDownLatch(1), disconnected = new CountDownLatch(1);
    HttpServer server;
    ExecutorService executor;
    ChatStreamClient client;
    volatile String mode="normal";
    @BeforeEach void setup() throws Exception {
        server=HttpServer.create(new InetSocketAddress("127.0.0.1",0),0);
        executor=Executors.newVirtualThreadPerTaskExecutor(); server.setExecutor(executor);
        server.createContext("/internal/v1/chat/stream", exchange -> {
            calls.incrementAndGet();
            assertThat(exchange.getRequestHeaders().getFirst("X-Internal-Service-Token")).isEqualTo("service-secret");
            assertThat(exchange.getRequestHeaders().getFirst("X-Request-ID")).isEqualTo("stream-trace");
            var payload=mapper.readTree(exchange.getRequestBody().readAllBytes());
            assertThat(payload.path("session_token").asText()).isEqualTo("real-session");
            assertThat(payload.has("account_id")).isFalse();
            String execution=payload.path("execution_id").asText();
            assertThat(execution).matches("[a-f0-9]{32}");
            if (mode.equals("balance")) {
                exchange.getResponseHeaders().set("Content-Type","application/json");
                byte[] bytes="{\"code\":\"INSUFFICIENT_BALANCE\",\"detail\":\"private error\"}".getBytes(StandardCharsets.UTF_8);
                exchange.sendResponseHeaders(402,bytes.length); exchange.getResponseBody().write(bytes); exchange.close(); return;
            }
            exchange.getResponseHeaders().set("Content-Type",mode.equals("mime")?"text/plain":"text/event-stream");
            exchange.sendResponseHeaders(200,0);
            try {
                var scope=mapper.createObjectNode().put("account_id",mode.equals("owner")?99:42).put("candidate_id",7)
                    .put("session_id","chat-1").put("execution_id",execution);
                send(exchange,"scope",scope.toString());
                if (mode.equals("malformed")) { send(exchange,"token","{\"content\":\"a\",\"content\":\"private\"}"); return; }
                if (mode.equals("unknown")) { send(exchange,"raw_tool_output","{\"secret\":\"private\"}"); return; }
                if (mode.equals("oversize")) { send(exchange,"token","{\"content\":\""+"x".repeat(ChatSessionClient.MAX_BYTES)+"\"}"); return; }
                send(exchange,"token","{\"content\":\"First\"}"); tokenSent.countDown();
                if (mode.equals("slow")) allowFinal.await(3,TimeUnit.SECONDS);
                if (mode.equals("disconnect") || mode.equals("deadline")) {
                    for(int i=0;i<300;i++) { exchange.getResponseBody().write(": keepalive\n\n".getBytes(StandardCharsets.UTF_8)); exchange.getResponseBody().flush(); Thread.sleep(20); }
                    return;
                }
                if (mode.equals("eof")) return;
                if (mode.equals("error")) { send(exchange,"error","{\"detail\":\"private model URL\",\"tool_outputs\":\"private\"}"); return; }
                // The Spring SSE reader must reassemble fragmented CRLF/multiline data.
                exchange.getResponseBody().write("event: to".getBytes(StandardCharsets.UTF_8)); exchange.getResponseBody().flush();
                exchange.getResponseBody().write("ken\r\ndata: {\"content\":\r\ndata: \"上海\"}\r\n\r\n".getBytes(StandardCharsets.UTF_8)); exchange.getResponseBody().flush();
                var reply=ChatSessionClientTest.reply();
                if(mode.equals("profile")) ((ObjectNode)reply.get("profile")).put("id",99);
                send(exchange,"final",reply.toString());
            } catch(IOException error) { disconnected.countDown();
            } catch(InterruptedException error) { Thread.currentThread().interrupt();
            } finally { exchange.close(); }
        });
        server.start(); client=client(300000);
    }
    ChatStreamClient client(int timeout) {
        return new ChatStreamClient(mapper,"http://127.0.0.1:"+server.getAddress().getPort(),"service-secret","test","http://localhost:8000",timeout);
    }
    static void send(com.sun.net.httpserver.HttpExchange exchange,String name,String json) throws IOException {
        exchange.getResponseBody().write(("event: "+name+"\ndata: "+json+"\n\n").getBytes(StandardCharsets.UTF_8));
        exchange.getResponseBody().flush();
    }
    ObjectNode input() { return mapper.createObjectNode().put("candidate_id",7).put("session_id","chat-1").put("message","Hello"); }
    @AfterEach void close() { allowFinal.countDown(); if(server!=null)server.stop(0); if(executor!=null)executor.shutdownNow(); }
    @Test void relaysIncrementallyWithoutWaitingForFinalAndPreservesUnicode() throws Exception {
        mode="slow";
        var response=new MockHttpServletResponse();
        Future<?> task=executor.submit(() -> client.relay("real-session",42,input(),"stream-trace",response));
        assertThat(tokenSent.await(3,TimeUnit.SECONDS)).isTrue();
        long end=System.nanoTime()+TimeUnit.SECONDS.toNanos(2);
        while(!response.getContentAsString().contains("event: token") && System.nanoTime()<end)Thread.sleep(10);
        assertThat(response.getContentAsString()).contains("First").doesNotContain("event: final","event: scope","account_id","session_token");
        assertThat(task.isDone()).isFalse(); allowFinal.countDown(); task.get(5,TimeUnit.SECONDS);
        assertThat(response.getContentAsString()).contains("上海","event: final");
        assertThat(response.getHeader("Cache-Control")).isEqualTo("no-store");
        assertThat(calls.get()).isEqualTo(1);
    }
    @Test void rejectsWrongScopeAndPreservesBalanceBeforeCommittingHeaders() {
        for(String value:new String[]{"owner","mime","balance"}) {
            mode=value; var response=new MockHttpServletResponse();
            assertThatThrownBy(() -> client.relay("real-session",42,input(),"stream-trace",response))
                .isInstanceOfSatisfying(AuthException.class,e -> assertThat(e.status().value()).isEqualTo(value.equals("balance")?402:503))
                .hasMessageNotContaining("private");
            assertThat(response.isCommitted()).isFalse();
        }
        assertThat(calls.get()).isEqualTo(3);
    }
    @Test void malformedEventsTruncationAndBadFinalNeverReturnSuccessOrSecrets() throws Exception {
        for(String value:new String[]{"malformed","unknown","oversize","eof","profile","error"}) {
            mode=value; var response=new MockHttpServletResponse();
            client.relay("real-session",42,input(),"stream-trace",response);
            assertThat(response.getContentAsString()).contains("event: error").doesNotContain("event: final","private","event: raw_tool_output");
        }
        assertThat(calls.get()).isEqualTo(6);
        mode="normal"; var recovered=new MockHttpServletResponse();client.relay("real-session",42,input(),"stream-trace",recovered);
        assertThat(recovered.getContentAsString()).contains("event: final");
    }
    @Test void downstreamDisconnectCancelsUpstreamAndReleasesSlot() throws Exception {
        mode="disconnect";
        var response=new MockHttpServletResponse() {
            @Override public jakarta.servlet.ServletOutputStream getOutputStream() {
                var delegate=super.getOutputStream();
                return new jakarta.servlet.ServletOutputStream() {
                    @Override public boolean isReady() { return true; }
                    @Override public void setWriteListener(jakarta.servlet.WriteListener listener) { }
                    @Override public void write(int b) throws IOException {
                        if(getContentAsByteArray().length>60) throw new IOException("browser disconnected");
                        delegate.write(b);
                    }
                };
            }
        };
        client.relay("real-session",42,input(),"stream-trace",response);
        assertThat(disconnected.await(4,TimeUnit.SECONDS)).isTrue();
        assertThat(calls.get()).isEqualTo(1);
    }
    @Test void deadlineProducesOneErrorAndCancelsInsteadOfReplaying() throws Exception {
        mode="deadline"; client=client(500); var response=new MockHttpServletResponse();
        client.relay("real-session",42,input(),"stream-trace",response);
        assertThat(response.getContentAsString()).contains("event: error").doesNotContain("event: final");
        assertThat(disconnected.await(4,TimeUnit.SECONDS)).isTrue(); assertThat(calls.get()).isEqualTo(1);
    }
}
