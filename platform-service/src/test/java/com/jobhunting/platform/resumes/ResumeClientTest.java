package com.jobhunting.platform.resumes;

import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicInteger;
import com.jobhunting.platform.auth.AuthException;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockMultipartFile;
import tools.jackson.databind.ObjectMapper;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class ResumeClientTest {
    final ObjectMapper mapper = new ObjectMapper();
    final AtomicInteger calls = new AtomicInteger();
    HttpServer server;
    ResumeClient client;
    String body;
    int status = 200;

    @BeforeEach void setup() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/internal/v1/resumes/upload", exchange -> {
            calls.incrementAndGet();
            String payload = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            assertThat(payload).contains("session_token", "real-session", "candidate_id", "7", "resume.docx", "resume-bytes");
            assertThat(exchange.getRequestHeaders().getFirst("X-Internal-Service-Token")).isEqualTo("secret");
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(status, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        server.start();
        client = new ResumeClient(mapper, "http://127.0.0.1:" + server.getAddress().getPort(), "secret", "development", "http://localhost:8000");
        body = validBody();
    }

    @AfterEach void close() { server.stop(0); }

    private String validBody() {
        return "{\"account_id\":42,\"artifact\":{"
            + "\"id\":11,\"candidate_id\":7,\"job_id\":null,\"draft_id\":null,\"parent_artifact_id\":null,"
            + "\"version\":1,\"artifact_type\":\"source\",\"original_filename\":\"resume.docx\","
            + "\"download_filename\":\"resume.docx\",\"media_type\":\"application/vnd.openxmlformats-officedocument.wordprocessingml.document\","
            + "\"file_size\":12,\"sha256\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\","
            + "\"extraction_method\":\"docx\",\"text_length\":12,\"page_count\":null,\"status\":\"ready\","
            + "\"long_text_id\":null,\"created_at\":\"now\",\"knowledge_asset_id\":null,\"knowledge_asset_version_id\":null,"
            + "\"scan_status\":\"clean\",\"scan_engine\":\"clamav-private\",\"scan_reason\":\"private\","
            + "\"download_url\":\"/api/resumes/11/download\"},\"rag_update\":{\"document_count\":1,\"chunk_count\":2,\"mode\":\"incremental\",\"persist_directory\":\"private\"},"
            + "\"warning\":\"private provider detail\",\"task\":null,\"workflow\":\"ready\",\"processing_async\":false,"
            + "\"indexing_async\":false,\"root_request_id\":null}";
    }

    @Test void forwardsOneNonReplayableFileAndSanitizesPrivateProjection() {
        var file = new MockMultipartFile("file", "C:\\private\\resume.docx", "application/octet-stream", "resume-bytes".getBytes(StandardCharsets.UTF_8));
        var result = client.upload("real-session", 42, 7, file, "trace");
        assertThat(result.has("account_id")).isFalse();
        assertThat(result.path("warning").asText()).doesNotContain("private");
        assertThat(result.path("rag_update").has("persist_directory")).isFalse();
        assertThat(result.path("artifact").path("scan_engine").isNull()).isTrue();
        assertThat(result.path("artifact").path("scan_reason").isNull()).isTrue();
        assertThat(calls.get()).isEqualTo(1);
    }

    @Test void rejectsOversizedFileBeforeCallingPython() {
        var file = new MockMultipartFile("file", "resume.pdf", "application/pdf", new byte[(int) ResumeClient.MAX_FILE_BYTES + 1]);
        assertThatThrownBy(() -> client.upload("real-session", 42, 7, file, "trace"))
            .isInstanceOf(AuthException.class).hasMessageContaining("20 MB");
        assertThat(calls.get()).isZero();
    }

    @Test void redactsCallbackErrorsAndRejectsMalformedSuccess() {
        status = 409;
        body = "{\"code\":\"DUPLICATE_RESUME\",\"detail\":\"private storage path\"}";
        assertThatThrownBy(() -> client.upload("real-session", 42, 7,
                new MockMultipartFile("file", "resume.pdf", "application/pdf", "x".getBytes()), "trace"))
            .isInstanceOf(AuthException.class).hasMessageNotContaining("private");
        status = 200;
        body = "{\"account_id\":42,\"artifact\":{}}";
        assertThatThrownBy(() -> client.upload("real-session", 42, 7,
                new MockMultipartFile("file", "resume.pdf", "application/pdf", "x".getBytes()), "trace"))
            .isInstanceOf(AuthException.class);
    }
}
