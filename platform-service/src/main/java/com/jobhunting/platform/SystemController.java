package com.jobhunting.platform;

import java.util.Map;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/internal/v1")
public class SystemController {

    private final String applicationName;
    private final String applicationVersion;

    public SystemController(
            @Value("${spring.application.name}") String applicationName,
            @Value("${app.version}") String applicationVersion) {
        this.applicationName = applicationName;
        this.applicationVersion = applicationVersion;
    }

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "ok", "service", applicationName);
    }

    @GetMapping("/version")
    public Map<String, String> version() {
        return Map.of(
                "service", applicationName,
                "version", applicationVersion);
    }
}
