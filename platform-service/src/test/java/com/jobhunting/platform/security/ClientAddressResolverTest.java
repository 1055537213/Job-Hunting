package com.jobhunting.platform.security;

import static org.assertj.core.api.Assertions.*;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockHttpServletRequest;

class ClientAddressResolverTest {
    private MockHttpServletRequest request(String peer, String forwarded) {
        var request = new MockHttpServletRequest();
        request.setRemoteAddr(peer);
        if (forwarded != null) request.addHeader("X-Forwarded-For", forwarded);
        return request;
    }

    @Test void defaultsIgnoreSpoofedHeadersIncludingMalformedValues() {
        assertThat(new ClientAddressResolver("").resolve(request("203.0.113.1", "evil.invalid"))).isEqualTo("203.0.113.1");
        assertThat(new ClientAddressResolver("10.0.0.0/24").resolve(request("203.0.113.1", "1.2.3.4"))).isEqualTo("203.0.113.1");
    }

    @Test void walksTrustedChainFromRightNotAttackerControlledLeft() {
        var resolver = new ClientAddressResolver("10.0.0.0/24,2001:db8::/64");
        assertThat(resolver.resolve(request("10.0.0.2", "1.2.3.4, 203.0.113.8, 10.0.0.3"))).isEqualTo("203.0.113.8");
        assertThat(resolver.resolve(request("2001:db8::2", "2001:db8:1::9"))).contains("2001:db8:1:");
        assertThat(resolver.resolve(request("10.0.0.2", null))).isEqualTo("10.0.0.2");
        assertThat(resolver.resolve(request("10.0.0.2", "::ffff:203.0.113.8"))).isEqualTo("203.0.113.8");
    }

    @Test void rejectsMalformedOversizedAndDuplicateChainsWithoutDnsLookups() {
        var resolver = new ClientAddressResolver("127.0.0.1");
        for (String header : new String[]{"localhost", "1.2.3.999", "1.2.3.4,", "[::1]:123", "::1%eth0", "01.2.3.4",
                "1.2.3.4,".repeat(17), "1".repeat(2049)}) {
            assertThatThrownBy(() -> resolver.resolve(request("127.0.0.1", header))).isInstanceOf(ClientAddressResolver.InvalidAddress.class);
        }
        var request = request("127.0.0.1", "1.2.3.4");
        request.addHeader("X-Forwarded-For", "5.6.7.8");
        assertThatThrownBy(() -> resolver.resolve(request)).isInstanceOf(ClientAddressResolver.InvalidAddress.class);
    }

    @Test void configurationRejectsGlobalTrustHostnamesAndInvalidNetworks() {
        for (String mask : new String[]{"0.0.0.0/0", "::/0", "localhost/32", "10.0.0.1/33", "10.0.0.1,", "10.0.0.1/+0"}) {
            assertThatThrownBy(() -> new ClientAddressResolver(mask)).isInstanceOf(IllegalArgumentException.class);
        }
    }
}
