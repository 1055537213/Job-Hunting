package com.jobhunting.platform.resumes;

import java.util.Set;
import tools.jackson.databind.JsonNode;

/** Validates the narrow public projection returned by the Python document pipeline. */
final class ResumeProjection {
    private static final Set<String> ARTIFACT_FIELDS = Set.of(
            "id", "candidate_id", "job_id", "draft_id", "parent_artifact_id", "version", "artifact_type",
            "original_filename", "download_filename", "media_type", "file_size", "sha256", "extraction_method",
            "text_length", "page_count", "status", "long_text_id", "created_at", "knowledge_asset_id",
            "knowledge_asset_version_id", "scan_status", "scan_engine", "scan_reason", "download_url");
    private static final Set<String> ROOT_FIELDS = Set.of(
            "artifact", "rag_update", "warning", "task", "workflow", "processing_async", "indexing_async", "root_request_id");
    private static final Set<String> TASK_FIELDS = Set.of(
            "task_key", "task_type", "status", "progress", "attempt", "max_attempts", "result", "error_summary",
            "created_at", "started_at", "finished_at", "updated_at");

    private ResumeProjection() {}

    static void validate(JsonNode value, long candidateId) {
        require(fields(value, ROOT_FIELDS));
        JsonNode artifact = value.get("artifact");
        require(artifact != null && artifact.isObject() && fields(artifact, ARTIFACT_FIELDS));
        require(id(artifact.get("id")) && id(artifact.get("candidate_id"))
                && artifact.get("candidate_id").asLong() == candidateId
                && positiveOrNull(artifact.get("job_id")) && positiveOrNull(artifact.get("draft_id"))
                && positiveOrNull(artifact.get("parent_artifact_id"))
                && artifact.get("version").isIntegralNumber() && artifact.get("version").asLong() > 0
                && text(artifact, "artifact_type") && text(artifact, "original_filename")
                && text(artifact, "download_filename") && text(artifact, "media_type")
                && artifact.get("file_size").isIntegralNumber() && artifact.get("file_size").asLong() >= 0
                && artifact.get("sha256").isTextual() && artifact.get("sha256").asText().matches("[A-Fa-f0-9]{64}")
                && text(artifact, "extraction_method") && artifact.get("text_length").isIntegralNumber()
                && artifact.get("text_length").asLong() >= 0 && integralOrNull(artifact.get("page_count"))
                && text(artifact, "status") && positiveOrNull(artifact.get("long_text_id"))
                && text(artifact, "created_at") && positiveOrNull(artifact.get("knowledge_asset_id"))
                && positiveOrNull(artifact.get("knowledge_asset_version_id")) && text(artifact, "scan_status")
                && textOrNull(artifact.get("scan_engine")) && textOrNull(artifact.get("scan_reason"))
                && artifact.get("download_url").isTextual()
                && artifact.get("download_url").asText().equals("/api/resumes/" + artifact.get("id").asLong() + "/download"));
        require(value.get("rag_update").isNull() || value.get("rag_update").isObject());
        require(value.get("warning").isNull() || boundedText(value.get("warning"), 512));
        require(value.get("task").isNull() || task(value.get("task")));
        require(value.get("workflow").isTextual() && Set.of("ready", "ocr", "rag").contains(value.get("workflow").asText()));
        require(value.get("processing_async").isBoolean() && value.get("indexing_async").isBoolean());
        require(value.get("root_request_id").isNull()
                || value.get("root_request_id").isTextual() && value.get("root_request_id").asText().matches("[A-Fa-f0-9]{32}"));
    }

    private static boolean task(JsonNode value) {
        return fields(value, TASK_FIELDS) && value.get("task_key").isTextual()
                && value.get("task_key").asText().matches("[A-Za-z0-9_-]{1,128}")
                && value.get("task_type").isTextual()
                && Set.of("resume_ocr", "rag_index").contains(value.get("task_type").asText())
                && value.get("status").isTextual()
                && Set.of("queued", "running", "succeeded", "failed", "cancelled").contains(value.get("status").asText())
                && value.get("progress").isIntegralNumber() && value.get("progress").asInt() >= 0
                && value.get("progress").asInt() <= 100 && value.get("attempt").isIntegralNumber()
                && value.get("attempt").asLong() >= 0 && id(value.get("max_attempts"))
                && (value.get("result").isNull() || value.get("result").isObject() && value.get("result").size() == 0)
                && text(value, "created_at") && textOrNull(value.get("started_at")) && textOrNull(value.get("finished_at"))
                && text(value, "updated_at") && textOrNull(value.get("error_summary"));
    }

    private static boolean fields(JsonNode value, Set<String> expected) {
        return value != null && value.isObject() && value.propertyNames().equals(expected);
    }
    private static boolean id(JsonNode value) { return value != null && value.isIntegralNumber() && value.canConvertToLong() && value.asLong() > 0; }
    private static boolean positiveOrNull(JsonNode value) { return value.isNull() || id(value); }
    private static boolean integralOrNull(JsonNode value) { return value.isNull() || value.isIntegralNumber() && value.asLong() >= 0; }
    private static boolean text(JsonNode value, String name) { return value.get(name).isTextual() && !value.get(name).asText().isBlank(); }
    private static boolean textOrNull(JsonNode value) { return value.isNull() || value.isTextual(); }
    private static boolean boundedText(JsonNode value, int max) { return value.isTextual() && value.asText().length() <= max; }
    private static void require(boolean condition) { if (!condition) throw ResumeClient.unavailable(); }
}
