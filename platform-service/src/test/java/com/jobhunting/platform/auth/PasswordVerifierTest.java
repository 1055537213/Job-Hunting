package com.jobhunting.platform.auth;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class PasswordVerifierTest {

    private final PasswordVerifier verifier = new PasswordVerifier();

    @Test
    void verifiesPythonArgon2idAndScryptHashes() {
        String argon2 = "$argon2id$v=19$m=65536,t=3,p=4$HrqMoOZsjnzZk6mfxO3ZSw$"
                + "6VI7r4COkxseS05eWFPkNvuJF3WICBQuGpGaGxTwWt4";
        String scrypt = "scrypt$1$30313233343536373839616263646566$"
                + "1907c58f5f54064b4a163ed317d124ba375d8e2ad9e13506f8ee8547a765986a";

        assertTrue(verifier.matches("password-123", argon2));
        assertTrue(verifier.matches("password-123", scrypt));
        assertFalse(verifier.matches("wrong-password", argon2));
        assertFalse(verifier.matches("wrong-password", scrypt));
    }
}
