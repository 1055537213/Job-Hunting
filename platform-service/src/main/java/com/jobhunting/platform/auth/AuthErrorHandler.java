package com.jobhunting.platform.auth;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
public class AuthErrorHandler {
    @ExceptionHandler(AuthException.class)
    public ResponseEntity<ErrorResponse> authError(AuthException exception, HttpServletRequest request) {
        String trace = request.getHeader("X-Trace-Id");
        return ResponseEntity.status(exception.status()).body(new ErrorResponse(
                exception.code(), exception.getMessage(), trace == null ? "missing" : trace));
    }
    public record ErrorResponse(String code, String message, String trace_id) { }
}
