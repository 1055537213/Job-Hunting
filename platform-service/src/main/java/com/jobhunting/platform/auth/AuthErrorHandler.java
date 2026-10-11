package com.jobhunting.platform.auth;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.bind.MethodArgumentNotValidException;

@RestControllerAdvice
public class AuthErrorHandler {
    @ExceptionHandler(AuthException.class)
    public ResponseEntity<ErrorResponse> authError(AuthException exception, HttpServletRequest request) {
        String trace = request.getHeader("X-Trace-Id");
        return ResponseEntity.status(exception.status()).body(new ErrorResponse(
                exception.code(), exception.getMessage(), exception.getMessage(), trace == null ? "missing" : trace));
    }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<ErrorResponse> validationError(
            MethodArgumentNotValidException exception, HttpServletRequest request) {
        String detail = exception.getBindingResult().getFieldErrors().stream()
                .findFirst()
                .map(error -> error.getField() + " 参数无效。")
                .orElse("请求参数无效。")
                .strip();
        String trace = request.getHeader("X-Trace-Id");
        return ResponseEntity.status(HttpStatus.UNPROCESSABLE_ENTITY).body(new ErrorResponse(
                "INVALID_REQUEST", detail, detail, trace == null ? "missing" : trace));
    }

    public record ErrorResponse(String code, String message, String detail, String trace_id) { }
}
