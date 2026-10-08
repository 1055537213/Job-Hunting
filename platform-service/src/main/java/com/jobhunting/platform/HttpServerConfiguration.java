package com.jobhunting.platform;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.tomcat.servlet.TomcatServletWebServerFactory;
import org.springframework.boot.web.server.WebServerFactoryCustomizer;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/** Allows local environments to select a Tomcat connector compatible with their OS. */
@Configuration
public class HttpServerConfiguration {

    @Bean
    WebServerFactoryCustomizer<TomcatServletWebServerFactory> tomcatProtocolCustomizer(
            @Value("${server.tomcat.protocol:org.apache.coyote.http11.Http11NioProtocol}") String protocol) {
        return factory -> factory.setProtocol(protocol);
    }
}
