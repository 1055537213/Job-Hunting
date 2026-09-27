package com.jobhunting.platform.billing;

import org.springframework.http.HttpStatus;

public class BillingException extends RuntimeException {

    private final String code;
    private final HttpStatus status;

    public BillingException(String code, String message, HttpStatus status) {
        super(message);
        this.code = code;
        this.status = status;
    }

    public String code() {
        return code;
    }

    public HttpStatus status() {
        return status;
    }
}
