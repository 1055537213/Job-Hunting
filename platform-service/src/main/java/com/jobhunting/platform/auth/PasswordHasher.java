package com.jobhunting.platform.auth;

import org.springframework.security.crypto.argon2.Argon2PasswordEncoder;
import org.springframework.stereotype.Component;

/** Creates password hashes in the same portable format understood by Python. */
@Component
public class PasswordHasher {
    private final Argon2PasswordEncoder argon2 = Argon2PasswordEncoder.defaultsForSpringSecurity_v5_8();

    public String encode(String password) {
        return argon2.encode(password);
    }
}
