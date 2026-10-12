package com.jobhunting.platform.security;

import java.net.InetAddress;
import java.net.UnknownHostException;
import java.util.ArrayList;
import java.util.List;
import jakarta.servlet.http.HttpServletRequest;
import org.apache.catalina.util.NetMask;

/** Only explicitly trusted direct peers may supply a bounded, literal-IP forwarding chain. */
public final class ClientAddressResolver {
    private final List<NetMask> trusted;

    public ClientAddressResolver(String cidrs) {
        var masks = new ArrayList<NetMask>();
        if (cidrs != null && !cidrs.isBlank()) {
            String[] entries = cidrs.split(",", -1);
            if (entries.length > 32) throw new IllegalArgumentException("Too many trusted proxies");
            for (String entry : entries) {
                String[] parts = entry.strip().split("/", -1);
                try {
                    literal(parts[0]);
                    if (parts.length > 2 || (parts.length == 2
                            && (!parts[1].matches("[0-9]{1,3}") || Integer.parseInt(parts[1]) == 0))) {
                        throw new IllegalArgumentException("Invalid trusted proxy mask");
                    }
                    masks.add(new NetMask(entry.strip()));
                } catch (InvalidAddress error) {
                    throw new IllegalArgumentException("Trusted proxies must be literal IP addresses or CIDRs");
                }
            }
        }
        trusted = List.copyOf(masks);
    }

    public boolean configured() { return !trusted.isEmpty(); }

    public String resolve(HttpServletRequest request) {
        InetAddress peer = literal(request.getRemoteAddr());
        if (!isTrusted(peer)) return peer.getHostAddress();
        var headers = request.getHeaders("X-Forwarded-For");
        if (headers == null || !headers.hasMoreElements()) return peer.getHostAddress();
        String header = headers.nextElement();
        if (headers.hasMoreElements() || header == null || header.length() > 2048) throw new InvalidAddress();
        String[] hops = header.split(",", -1);
        if (hops.length > 16) throw new InvalidAddress();
        // Validate the whole chain, then walk right-to-left until the first untrusted hop.
        var addresses = new ArrayList<InetAddress>();
        for (String hop : hops) addresses.add(literal(hop.strip()));
        for (int i = addresses.size() - 1; i >= 0 && isTrusted(peer); i--) peer = addresses.get(i);
        return peer.getHostAddress();
    }

    private boolean isTrusted(InetAddress address) {
        return trusted.stream().anyMatch(mask -> mask.matches(address));
    }

    private static InetAddress literal(String value) {
        if (value == null || value.length() > 45) throw new InvalidAddress();
        if (value.contains(":")) {
            if (!value.matches("[0-9A-Fa-f:.]+")) throw new InvalidAddress();
        } else {
            if (!value.matches("(?:0|[1-9][0-9]{0,2})(?:\\.(?:0|[1-9][0-9]{0,2})){3}")) throw new InvalidAddress();
            for (String part : value.split("\\.")) if (Integer.parseInt(part) > 255) throw new InvalidAddress();
        }
        try {
            return InetAddress.getByName(value);
        } catch (UnknownHostException error) {
            throw new InvalidAddress();
        }
    }

    public static final class InvalidAddress extends RuntimeException {
        public InvalidAddress() { super("Invalid client address"); }
    }
}
