package com.jobhunting.platform.auth;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HexFormat;

import org.bouncycastle.crypto.generators.SCrypt;
import org.springframework.security.crypto.argon2.Argon2PasswordEncoder;
import org.springframework.stereotype.Component;

/** Reads the existing Python hashes without rewriting users' credentials. */
@Component
public class PasswordVerifier {
    private final Argon2PasswordEncoder argon2 = Argon2PasswordEncoder.defaultsForSpringSecurity_v5_8();

    public boolean matches(String password, String encoded) {
        if (password == null || encoded == null) {
            return false;
        }
        try {
            if (encoded.startsWith("$argon2")) {
                return argon2.matches(password, encoded);
            }
            if (encoded.startsWith("scrypt$1$")) {
                String[] parts = encoded.split("\\$", -1);
                if (parts.length != 4 || parts[2].length() != 32 || parts[3].length() != 64) {
                    return false;
                }
                byte[] salt = HexFormat.of().parseHex(parts[2]);
                byte[] expected = HexFormat.of().parseHex(parts[3]);
                byte[] actual = SCrypt.generate(password.getBytes(StandardCharsets.UTF_8), salt, 32768, 8, 1, 32);
                return MessageDigest.isEqual(expected, actual);
            }
        } catch (IllegalArgumentException exception) {
            return false;
        }
        return false;
    }
}
