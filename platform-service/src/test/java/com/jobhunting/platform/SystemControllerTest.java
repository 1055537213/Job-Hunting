package com.jobhunting.platform;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(SystemController.class)
class SystemControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void healthReportsThePlatformService() throws Exception {
        mockMvc.perform(get("/internal/v1/health"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("ok"))
                .andExpect(jsonPath("$.service").value("job-hunting-platform"));
    }

    @Test
    void versionReportsTheConfiguredApplicationVersion() throws Exception {
        mockMvc.perform(get("/internal/v1/version"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.version").value("0.1.0"));
    }
}
