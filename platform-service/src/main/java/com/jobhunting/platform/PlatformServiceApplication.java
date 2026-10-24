package com.jobhunting.platform;

import java.security.Security;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class PlatformServiceApplication {

    public static void main(String[] args) {
        // Docker service names can briefly disappear during a dependency restart. Do not
        // cache that transient NXDOMAIN result, or a recovered Python service stays
        // unreachable until the JVM negative-DNS TTL expires.
        Security.setProperty("networkaddress.cache.negative.ttl", "0");
        SpringApplication.run(PlatformServiceApplication.class, args);
    }
}
