package com.jobhunting.platform.jobs;

import java.util.Set;
import tools.jackson.databind.JsonNode;

/** Validates the public job projection returned by Python screenshot analysis. */
final class JobProjection {
    private static final Set<String> JOB_FIELDS = Set.of(
            "id", "raw_text", "source_url", "title", "city", "salary_min_k", "salary_max_k", "salary_months",
            "salary_unit", "experience_min_years", "experience_max_years", "experience_label", "education",
            "company_name", "industry", "company_size", "skills", "description_text", "field_confidence",
            "uncertainty_notes", "skill_requirements", "import_method", "captured_at");
    private static final Set<String> REQUIREMENT_FIELDS = Set.of("name", "category", "confidence", "evidence");

    private JobProjection() {}

    static void validate(JsonNode value) {
        require(value != null && value.isObject() && value.propertyNames().equals(Set.of("job", "extraction")));
        validateJob(value.get("job"));
        JsonNode extraction = value.get("extraction");
        require(extraction.isObject() && extraction.propertyNames().equals(Set.of("source", "screenshot_count"))
                && extraction.get("source").isTextual() && extraction.get("source").asText().equals("screenshot")
                && extraction.get("screenshot_count").isIntegralNumber() && extraction.get("screenshot_count").asInt() >= 1
                && extraction.get("screenshot_count").asInt() <= 4);
    }

    static void validateText(JsonNode value) {
        require(value != null && value.isObject() && value.propertyNames().equals(Set.of("job")));
        validateJob(value.get("job"));
    }

    static void validateList(JsonNode value) {
        require(value != null && value.isObject() && value.propertyNames().equals(Set.of("jobs"))
                && value.get("jobs").isArray());
        for (JsonNode job : value.get("jobs")) validateJob(job);
    }

    static void validateSkillUpdate(JsonNode value) {
        require(value != null && value.isObject() && value.propertyNames().equals(Set.of("job")));
        validateJob(value.get("job"));
    }

    static void validateDelete(JsonNode value) {
        require(value != null && value.isObject() && value.propertyNames().equals(Set.of("deleted", "job_id"))
                && value.get("deleted").isBoolean() && value.get("deleted").asBoolean()
                && id(value.get("job_id")));
    }

    private static void validateJob(JsonNode job) {
        require(job != null && job.isObject() && job.propertyNames().equals(JOB_FIELDS)
                && id(job.get("id")) && text(job, "raw_text") && text(job, "title")
                && text(job, "salary_unit") && text(job, "import_method")
                && list(job.get("skills")) && list(job.get("uncertainty_notes"))
                && job.get("field_confidence").isObject() && text(job, "description_text")
                && list(job.get("skill_requirements")) && textOrNull(job.get("source_url"))
                && textOrNull(job.get("city")) && numberOrNull(job.get("salary_min_k"))
                && numberOrNull(job.get("salary_max_k")) && numberOrNull(job.get("salary_months"))
                && numberOrNull(job.get("experience_min_years")) && numberOrNull(job.get("experience_max_years"))
                && textOrNull(job.get("experience_label")) && textOrNull(job.get("education"))
                && textOrNull(job.get("company_name")) && textOrNull(job.get("industry"))
                && textOrNull(job.get("company_size")) && textOrNull(job.get("captured_at")));
        for (JsonNode requirement : job.get("skill_requirements")) {
            require(requirement.isObject() && requirement.propertyNames().equals(REQUIREMENT_FIELDS)
                    && text(requirement, "name") && text(requirement, "category")
                    && requirement.get("confidence").isNumber() && requirement.get("confidence").asDouble() >= 0
                    && requirement.get("confidence").asDouble() <= 1 && textOrNull(requirement.get("evidence")));
        }
    }

    private static boolean id(JsonNode value) { return value != null && value.isIntegralNumber() && value.canConvertToLong() && value.asLong() > 0; }
    private static boolean text(JsonNode value, String name) { return value.get(name).isTextual(); }
    private static boolean textOrNull(JsonNode value) { return value.isNull() || value.isTextual(); }
    private static boolean numberOrNull(JsonNode value) { return value.isNull() || value.isNumber(); }
    private static boolean list(JsonNode value) { return value != null && value.isArray(); }
    private static void require(boolean condition) { if (!condition) throw JobClient.unavailable(); }
}
