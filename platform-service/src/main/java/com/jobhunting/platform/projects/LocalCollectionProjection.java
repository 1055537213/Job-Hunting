package com.jobhunting.platform.projects;

import java.util.HashSet;
import java.util.Set;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ArrayNode;

final class LocalCollectionProjection {
    private LocalCollectionProjection(){}
    static final int MAX_BYTES=16*1024*1024;
    static final Set<String> COLLECTION=Set.of("id","candidate_id","project_card_id","project_name","source_type","manifest_fingerprint","status",
        "file_count","selected_file_count","uploaded_file_count","total_size","selected_size","error_summary","created_at","updated_at");
    static final Set<String> FILE=Set.of("id","collection_id","relative_path","file_kind","media_type","file_size","client_sha256","server_sha256",
        "selection_status","selection_reason","extraction_method","text_length","long_text_id","metadata","created_at","updated_at");
    static void manifest(JsonNode input){
        if(!input.path("project_name").isTextual() || input.get("project_name").asText().isBlank()
                || input.get("project_name").asText().codePointCount(0,input.get("project_name").asText().length())>256
                || !input.path("files").isArray() || input.get("files").isEmpty() || input.get("files").size()>20000)throw ProjectClient.invalid();
        for(var file:input.get("files")){
            if(!file.isObject() || !Set.of("relative_path","file_size","sha256","media_type","last_modified").containsAll(file.propertyNames())
                || !file.path("relative_path").isTextual() || file.get("relative_path").asText().isEmpty()
                || file.get("relative_path").asText().codePointCount(0,file.get("relative_path").asText().length())>2048
                || !number(file.get("file_size")) || file.get("file_size").asLong()>(1L<<48)
                || file.hasNonNull("sha256") && (!file.get("sha256").isTextual()||!file.get("sha256").asText().matches("[a-fA-F0-9]{64}"))
                || file.has("media_type") && (!file.get("media_type").isTextual()||file.get("media_type").asText().length()>128)
                || file.hasNonNull("last_modified") && (!number(file.get("last_modified"))||file.get("last_modified").asLong()>9007199254740991L))throw ProjectClient.invalid();
        }
    }
    static void validate(JsonNode value,String operation,JsonNode input){
        if("local-cancel".equals(operation)){
            require(ProjectClient.fields(value,Set.of("deleted","collection_id","candidate_id","cleanup_status","rag_cleanup")));
            require(value.get("deleted").isBoolean()&&value.get("deleted").asBoolean()&&ProjectClient.id(value.get("collection_id"))
                &&value.get("collection_id").asLong()==input.path("collection_id").asLong()&&ProjectClient.id(value.get("candidate_id"))
                &&value.get("cleanup_status").isTextual()&&Set.of("pending","completed").contains(value.get("cleanup_status").asText())
                &&"database_cascade".equals(value.get("rag_cleanup").asText()));return;
        }
        boolean manifest="local-manifest".equals(operation);
        require(ProjectClient.fields(value,manifest?Set.of("collection","files","selected_files","resumed"):Set.of("collection","files")));
        var c=value.get("collection");require(ProjectClient.fields(c,COLLECTION));
        require(ProjectClient.id(c.get("id"))&&ProjectClient.id(c.get("candidate_id"))&&nullableId(c.get("project_card_id"))
            &&(manifest?c.get("candidate_id").asLong()==input.path("candidate_id").asLong():c.get("id").asLong()==input.path("collection_id").asLong())
            &&"local_directory_collection".equals(c.get("source_type").asText())&&c.get("status").isTextual()
            &&Set.of("planned","uploading","processing","ready","failed","cancelled").contains(c.get("status").asText()));
        for(var key:Set.of("project_name","manifest_fingerprint","created_at","updated_at"))require(c.get(key).isTextual());
        require(c.get("manifest_fingerprint").asText().matches("[a-f0-9]{64}"));
        require(c.get("error_summary").isNull() || "采集未完成，请重新选择目录后重试。".equals(c.get("error_summary").asText()));
        for(var key:Set.of("file_count","selected_file_count","uploaded_file_count","total_size","selected_size"))require(number(c.get(key)));
        var files=value.get("files");require(files.isArray()&&!files.isEmpty()&&files.size()<=20000&&c.get("file_count").asLong()==files.size());
        var ids=new HashSet<Long>();var paths=new HashSet<String>();long total=0;boolean analyzed=false;
        ArrayNode selected=tools.jackson.databind.node.JsonNodeFactory.instance.arrayNode();
        for(var f:files){
            require(ProjectClient.fields(f,FILE));
            require(ProjectClient.id(f.get("id"))&&ids.add(f.get("id").asLong())&&ProjectClient.id(f.get("collection_id"))&&f.get("collection_id").asLong()==c.get("id").asLong());
            for(var key:Set.of("relative_path","file_kind","media_type","selection_status","selection_reason","created_at","updated_at"))require(f.get(key).isTextual());
            require(!f.get("relative_path").asText().isEmpty()&&f.get("relative_path").asText().codePointCount(0,f.get("relative_path").asText().length())<=2048
                &&paths.add(f.get("relative_path").asText())&&Set.of("selected","skipped","uploaded","analyzed","failed").contains(f.get("selection_status").asText()));
            require(number(f.get("file_size"))&&number(f.get("text_length"))&&nullableId(f.get("long_text_id"))
                &&(f.get("extraction_method").isNull()||f.get("extraction_method").isTextual()));
            for(var key:Set.of("client_sha256","server_sha256"))require(f.get(key).isNull()||f.get(key).isTextual()&&f.get(key).asText().matches("[a-f0-9]{64}"));
            require(ProjectClient.fields(f.get("metadata"),Set.of("last_modified")));
            require(f.get("metadata").get("last_modified").isNull()||number(f.get("metadata").get("last_modified")));
            total=Math.addExact(total,f.get("file_size").asLong());
            if("selected".equals(f.get("selection_status").asText()))selected.add(f);
            analyzed|="analyzed".equals(f.get("selection_status").asText());
        }
        require(total==c.get("total_size").asLong()&&c.get("selected_file_count").asLong()<=files.size()
            &&c.get("uploaded_file_count").asLong()<=c.get("selected_file_count").asLong()&&c.get("selected_size").asLong()<=total);
        if(manifest)require(value.get("selected_files").equals(selected)&&value.get("resumed").isBoolean()
            &&value.get("resumed").asBoolean()==(c.get("uploaded_file_count").asLong()>0||analyzed));
    }
    private static boolean number(JsonNode n){return n!=null&&n.isIntegralNumber()&&n.canConvertToLong()&&n.asLong()>=0;}
    private static boolean nullableId(JsonNode n){return n!=null&&(n.isNull()||ProjectClient.id(n));}
    private static void require(boolean valid){if(!valid)throw ProjectClient.unavailable();}
}
