package com.jobhunting.platform.billing;

import jakarta.servlet.http.HttpServletRequest;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
public class BillingErrorHandler {

    @ExceptionHandler(BillingException.class)
    ResponseEntity<BillingDtos.ErrorResponse> handleBillingException(
            BillingException exception,
            HttpServletRequest request) {
        return ResponseEntity.status(exception.status())
                .body(new BillingDtos.ErrorResponse(
                        exception.code(),
                        exception.getMessage(),
                        traceId(request)));
    }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    ResponseEntity<BillingDtos.ErrorResponse> handleValidation(
            MethodArgumentNotValidException exception,
            HttpServletRequest request) {
        String message = exception.getBindingResult().getFieldErrors().stream()
                .findFirst()
                .map(error -> error.getField() + " 参数无效。")
                .orElse("请求参数无效。");
        return ResponseEntity.status(HttpStatus.UNPROCESSABLE_ENTITY)
                .body(new BillingDtos.ErrorResponse(
                        "INVALID_REQUEST",
                        message,
                        traceId(request)));
    }

    private String traceId(HttpServletRequest request) {
        String traceId = request.getHeader("X-Trace-Id");
        return traceId == null || traceId.isBlank() ? "missing" : traceId;
    }
}
