package com.jobhunting.platform.auth;

import java.util.Map;
import com.jobhunting.platform.billing.BillingException;
import com.jobhunting.platform.billing.BillingQueryController;
import com.jobhunting.platform.billing.PersonalRechargeController;
import com.jobhunting.platform.billing.AdminBillingController;
import com.jobhunting.platform.usage.AdminUsageController;
import com.jobhunting.platform.usage.AdminToolTraceController;
import com.jobhunting.platform.tasks.TaskReadController;
import com.jobhunting.platform.tasks.TaskCommandController;
import com.jobhunting.platform.observability.AdminObservabilityController;
import com.jobhunting.platform.security.WebSecurityFilter;
import com.jobhunting.platform.profiles.ProfileReadController;
import com.jobhunting.platform.profiles.ProfileCreateController;
import com.jobhunting.platform.profiles.ProfileDeleteController;
import com.jobhunting.platform.billing.PaymentWebhookController;
import com.jobhunting.platform.chat.ChatSessionController;
import com.jobhunting.platform.chat.ChatMessageController;
import com.jobhunting.platform.chat.ChatStreamController;
import com.jobhunting.platform.projects.ProjectController;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.MissingServletRequestParameterException;
import org.springframework.web.method.annotation.HandlerMethodValidationException;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/** Browser-compatible validation contract; billing writes keep their existing advice. */
@Order(Ordered.HIGHEST_PRECEDENCE)
@RestControllerAdvice(assignableTypes = {PublicAuthController.class, PublicAuthLifecycleController.class,
        PublicAdminAccountController.class, BillingQueryController.class, PersonalRechargeController.class, AdminBillingController.class,
        AdminUsageController.class, AdminToolTraceController.class, TaskReadController.class, TaskCommandController.class,
        AdminObservabilityController.class, PublicAccountProfileController.class, PublicAccountExportController.class,
        InternalAccountExportController.class, AccountDeletionController.class, ProfileReadController.class, ProfileCreateController.class, ProfileDeleteController.class,
        PaymentWebhookController.class, ChatSessionController.class, ChatMessageController.class, ChatStreamController.class, ProjectController.class})
public class PublicAuthErrorHandler {
    @ExceptionHandler(AuthException.class)
    public ResponseEntity<Map<String, Object>> auth(AuthException error, HttpServletRequest request) {
        String id = WebSecurityFilter.requestId(request.getHeader("X-Request-ID"), request.getHeader("X-Trace-Id"));
        var response = ResponseEntity.status(error.status()).header("Cache-Control", "no-store");
        if ("TASK_COMMAND_UNAVAILABLE".equals(error.code()) || "EXPORT_UNAVAILABLE".equals(error.code())
                || "PROFILE_QUERY_UNAVAILABLE".equals(error.code()) || "PROFILE_CREATE_UNAVAILABLE".equals(error.code())
                || "PROFILE_DELETE_UNAVAILABLE".equals(error.code()) || "PROFILE_DELETE_BUSY".equals(error.code())
                || "CHAT_UNAVAILABLE".equals(error.code()) || "CHAT_BUSY".equals(error.code())
                || "PROJECT_UNAVAILABLE".equals(error.code()) || "PROJECT_BUSY".equals(error.code())) response.header("Retry-After", "3");
        return response.body(Map.of("code", error.code(), "detail", error.getMessage(),
                "message", error.getMessage(), "request_id", id, "trace_id", id));
    }

    @ExceptionHandler(BillingException.class)
    public ResponseEntity<Map<String, Object>> billing(BillingException error, HttpServletRequest request) {
        String id = WebSecurityFilter.requestId(request.getHeader("X-Request-ID"), request.getHeader("X-Trace-Id"));
        return ResponseEntity.status(error.status()).body(Map.of("code", error.code(), "detail", error.getMessage(),
                "message", error.getMessage(), "request_id", id, "trace_id", id));
    }

    @ExceptionHandler({MethodArgumentNotValidException.class, HttpMessageNotReadableException.class,
            HandlerMethodValidationException.class, MethodArgumentTypeMismatchException.class, MissingServletRequestParameterException.class})
    public ResponseEntity<Map<String, Object>> invalid(Exception error, HttpServletRequest request) {
        String id = WebSecurityFilter.requestId(request.getHeader("X-Request-ID"), request.getHeader("X-Trace-Id"));
        String detail = "请求参数无效。";
        int status = error instanceof HttpMessageNotReadableException ? 400 : 422;
        return ResponseEntity.status(status).header("Cache-Control", "no-store").body(Map.of("code", "INVALID_REQUEST", "detail", detail,
                "message", detail, "request_id", id, "trace_id", id));
    }
}
