package com.jobhunting.platform.auth;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import com.jobhunting.platform.security.WebSecurityFilter;

@RestControllerAdvice
public class AuthErrorHandler {
    @ExceptionHandler(AuthException.class)
    public ResponseEntity<ErrorResponse> authError(AuthException exception, HttpServletRequest request) {
        String raw = request.getHeader("X-Trace-Id");
        String trace = raw != null && raw.matches("[A-Za-z0-9._:-]{1,128}")
                ? raw : WebSecurityFilter.requestId(request.getHeader("X-Request-ID"), null);
        return ResponseEntity.status(exception.status()).body(new ErrorResponse(
                exception.code(), exception.getMessage(), exception.getMessage(), trace));
    }

    public record ErrorResponse(String code, String message, String detail, String trace_id) { }
}
