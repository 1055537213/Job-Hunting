package com.jobhunting.platform.auth;

public interface CredentialVerifier {
    long verify(String email, String password, boolean verificationRequired);
}
