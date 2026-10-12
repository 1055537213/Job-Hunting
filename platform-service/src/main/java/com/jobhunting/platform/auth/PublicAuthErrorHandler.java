package com.jobhunting.platform.auth;

import java.util.Map;
import com.jobhunting.platform.security.WebSecurityFilter;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/** Browser-only validation contract; internal billing validation keeps its existing advice. */
@Order(Ordered.HIGHEST_PRECEDENCE)
@RestControllerAdvice(assignableTypes = PublicAuthController.class)
public class PublicAuthErrorHandler {
    @ExceptionHandler(AuthException.class)
    public ResponseEntity<Map<String, Object>> auth(AuthException error, HttpServletRequest request) {
        String id = WebSecurityFilter.requestId(request.getHeader("X-Request-ID"), request.getHeader("X-Trace-Id"));
        return ResponseEntity.status(error.status()).body(Map.of("code", error.code(), "detail", error.getMessage(),
                "message", error.getMessage(), "request_id", id, "trace_id", id));
    }

    @ExceptionHandler({MethodArgumentNotValidException.class, HttpMessageNotReadableException.class})
    public ResponseEntity<Map<String, Object>> invalid(Exception error, HttpServletRequest request) {
        String id = WebSecurityFilter.requestId(request.getHeader("X-Request-ID"), request.getHeader("X-Trace-Id"));
        String detail = "请求参数无效。";
        int status = error instanceof MethodArgumentNotValidException ? 422 : 400;
        return ResponseEntity.status(status).body(Map.of("code", "INVALID_REQUEST", "detail", detail,
                "message", detail, "request_id", id, "trace_id", id));
    }
}
