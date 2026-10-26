package com.jobhunting.platform.projects;

import java.util.HashSet;
import java.util.Set;
import tools.jackson.databind.JsonNode;

final class ProjectProjection {
    private ProjectProjection(){}
    private static final Set<String> LISTS=Set.of("read_files","detected_tech_stack","detected_core_features","responsibility_draft",
        "highlight_draft","resume_expression_draft","questions_for_candidate","deferred_files");
    static void validate(JsonNode value,String operation,JsonNode input){
        var expected=switch(operation){
            case "list"->Set.of("project_cards");case "confirm"->Set.of("project_card","task","root_request_id");
            case "delete"->Set.of("deleted","project_card_id","candidate_id","cleanup_status","rag_cleanup");
            case "github"->Set.of("task","project_card","processing_async","root_request_id");
            default->throw ProjectClient.unavailable();
        };
        require(ProjectClient.fields(value,expected));
        if("github".equals(operation)) {
            require(value.get("project_card").isNull() && value.get("processing_async").isBoolean() && value.get("processing_async").asBoolean()
                &&value.get("root_request_id").isTextual()&&value.get("root_request_id").asText().matches("[a-f0-9]{32}"));
            task(value.get("task"),"github_project_analysis");return;
        }
        if("delete".equals(operation)){
            require(value.get("deleted").isBoolean()&&value.get("deleted").asBoolean()&&ProjectClient.id(value.get("project_card_id"))&&value.get("project_card_id").asLong()==input.get("record_id").asLong()
                && ProjectClient.id(value.get("candidate_id"))&&value.get("cleanup_status").isTextual()
                &&Set.of("pending","completed").contains(value.get("cleanup_status").asText())&&"database_cascade".equals(value.get("rag_cleanup").asText()));return;
        }
        if("list".equals(operation)){
            require(value.get("project_cards").isArray());var seen=new HashSet<Long>();
            for(var row:value.get("project_cards")){record(row);require(row.get("candidate_id").asLong()==input.get("candidate_id").asLong()&&seen.add(row.get("id").asLong()));}
        }else{
            var row=value.get("project_card");record(row);
            require(row.get("id").asLong()==input.get("record_id").asLong()&&"已确认".equals(row.get("status").asText())
                &&row.get("confirmed_summary").isTextual()&&!row.get("confirmed_summary").asText().isBlank()&&row.get("confirmed_at").isTextual()
                &&value.get("root_request_id").isTextual()&&value.get("root_request_id").asText().matches("[a-f0-9]{32}"));
            var task=value.get("task");
            if(!task.isNull()){
                require(ProjectClient.fields(task,Set.of("task_key","task_type","status","progress","attempt","max_attempts","result","error_summary","created_at","started_at","finished_at","updated_at")));
                text(task,"task_key","task_type","status","created_at","updated_at");
                require(task.get("task_key").asText().matches("[A-Za-z0-9_-]{1,64}")&&"rag_index".equals(task.get("task_type").asText())
                    &&Set.of("queued","running","succeeded","failed","cancelled").contains(task.get("status").asText()));
                require(task.get("result").isNull()&&task.get("progress").isIntegralNumber()&&task.get("progress").asInt()>=0&&task.get("progress").asInt()<=100);
                for(var key:Set.of("error_summary","started_at","finished_at"))require(task.get(key).isNull()||task.get(key).isTextual());
                require(task.get("attempt").isIntegralNumber()&&task.get("attempt").asLong()>=0&&ProjectClient.id(task.get("max_attempts")));
            }
        }
    }
    private static void record(JsonNode row){
        require(ProjectClient.fields(row,Set.of("id","candidate_id","status","card","confirmed_summary","created_at","confirmed_at"))
            &&ProjectClient.id(row.get("id"))&&ProjectClient.id(row.get("candidate_id"))&&row.get("status").isTextual()
            &&Set.of("待确认","已确认").contains(row.get("status").asText()));
        text(row,"created_at");for(var k:Set.of("confirmed_summary","confirmed_at"))require(row.get(k).isNull()||row.get(k).isTextual());
        var card=row.get("card");var keys=new HashSet<>(LISTS);keys.addAll(Set.of("card_type","project_name","skipped_summary","source_type","source_url","source_ref","discovered_file_kinds"));
        require(ProjectClient.fields(card,keys));text(card,"card_type","project_name","source_type");
        for(var k:Set.of("source_url","source_ref"))require(card.get(k).isNull()||card.get(k).isTextual());
        for(var k:LISTS){require(card.get(k).isArray());for(var item:card.get(k))require(item.isTextual());}
        for(var k:Set.of("skipped_summary","discovered_file_kinds")){require(card.get(k).isObject());for(var n:card.get(k))require(n.isIntegralNumber()&&n.canConvertToInt()&&n.asInt()>=0);}
    }
    private static void text(JsonNode n,String...keys){for(var k:keys)require(n.get(k).isTextual());}
    private static void task(JsonNode task,String type){
        require(ProjectClient.fields(task,Set.of("task_key","task_type","status","progress","attempt","max_attempts","result","error_summary","created_at","started_at","finished_at","updated_at")));
        text(task,"task_key","task_type","status","created_at","updated_at");
        require(task.get("task_key").asText().matches("[A-Za-z0-9_-]{1,64}")&&type.equals(task.get("task_type").asText())
            &&Set.of("queued","running","succeeded","failed","cancelled").contains(task.get("status").asText())&&task.get("result").isNull()
            &&task.get("progress").isIntegralNumber()&&task.get("progress").canConvertToInt()&&task.get("progress").asInt()>=0&&task.get("progress").asInt()<=100
            &&task.get("attempt").isIntegralNumber()&&task.get("attempt").canConvertToLong()&&task.get("attempt").asLong()>=0&&ProjectClient.id(task.get("max_attempts")));
        for(var k:Set.of("error_summary","started_at","finished_at"))require(task.get(k).isNull()||task.get(k).isTextual());
    }
    private static void require(boolean condition){if(!condition)throw ProjectClient.unavailable();}
}
