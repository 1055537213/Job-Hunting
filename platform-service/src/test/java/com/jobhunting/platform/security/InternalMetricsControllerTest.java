package com.jobhunting.platform.security;

import static org.assertj.core.api.Assertions.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;
import java.nio.file.Files;
import java.nio.file.Path;
import io.micrometer.prometheusmetrics.PrometheusConfig;
import io.micrometer.prometheusmetrics.PrometheusMeterRegistry;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

class InternalMetricsControllerTest {
    private static final String TOKEN = "metrics-credential-with-at-least-32-characters";

    @Test void requiresIndependentBearerAndExportsRealRegistry() throws Exception {
        var registry = new PrometheusMeterRegistry(PrometheusConfig.DEFAULT);
        try {
            registry.counter("platform.web.requests", "group", "auth", "status", "2xx", "outcome", "handled").increment();
            var mvc = MockMvcBuilders.standaloneSetup(new InternalMetricsController(registry, TOKEN, "", "internal-token")).build();
            mvc.perform(get("/internal/metrics").accept("text/plain")).andExpect(status().isUnauthorized());
            mvc.perform(get("/internal/metrics").header("Authorization", "Bearer internal-token")).andExpect(status().isUnauthorized());
            mvc.perform(get("/internal/metrics").header("Authorization", "Bearer " + TOKEN))
                    .andExpect(status().isOk()).andExpect(header().string("Cache-Control", "no-store"))
                    .andExpect(content().string(org.hamcrest.Matchers.containsString("platform_web_requests_total")))
                    .andExpect(content().string(org.hamcrest.Matchers.not(org.hamcrest.Matchers.containsString(TOKEN))));
        } finally { registry.close(); }
    }

    @Test void readsComposeSecretAndRejectsWeakReusedOrUnreadableCredentials(@TempDir Path dir) throws Exception {
        var registry = new PrometheusMeterRegistry(PrometheusConfig.DEFAULT);
        try {
            Path file = dir.resolve("token");
            Files.writeString(file, TOKEN + "\n");
            assertThat(new InternalMetricsController(registry, "", file.toString(), "internal-token")
                    .scrape("Bearer " + TOKEN).getStatusCode().value()).isEqualTo(200);
            assertThatThrownBy(() -> new InternalMetricsController(registry, "short", "", ""))
                    .isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> new InternalMetricsController(registry, TOKEN, "", TOKEN))
                    .isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> new InternalMetricsController(registry, "", dir.resolve("missing").toString(), ""))
                    .isInstanceOf(IllegalArgumentException.class);
        } finally { registry.close(); }
    }
}
