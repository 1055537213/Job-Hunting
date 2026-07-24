"""对话式自动入库。

用户把资料发给 agent 后，这个模块负责判断哪些内容应该进入 SQLite 结构化档案，
哪些内容应该作为长文本材料进入后续 RAG 索引。第一版支持两种决策方式：

- 有 LLM：要求 LLM 返回 JSON 保存决策。
- 无 LLM：使用保守规则兜底，只提取明显事实。

无论哪种方式，最终落库都由 `JobHuntingApp` 和 `SQLiteStore` 执行，LLM 不直接写库。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .job_parser import EDUCATION_ORDER, KNOWN_SKILLS
from .llm import LLMClient
from .models import (
    CandidateProfile,
    CandidateProfilePatch,
    ConversationIngestionDecision,
    LongTextInput,
)


def decide_conversation_ingestion(
    candidate: CandidateProfile,
    message: str,
    llm_client: LLMClient | None = None,
) -> ConversationIngestionDecision:
    """为一条候选人资料消息生成保存决策。"""

    if llm_client is not None:
        llm_text = llm_client.complete(build_ingestion_prompt(candidate, message))
        try:
            return decision_from_json(candidate.id, message, llm_text)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError, AttributeError):
            # LLM 决策格式异常时回退规则决策，避免一次模型输出坏掉就丢失用户资料。
            fallback = rule_based_decision(candidate, message)
            fallback.reply = "我已保存你的原始资料；模型决策格式异常，本次仅按保守规则提取明确事实。"
            return fallback
    return rule_based_decision(candidate, message)


def build_ingestion_prompt(candidate: CandidateProfile, message: str) -> str:
    """构造要求 LLM 返回 JSON 保存决策的 prompt。"""

    return f"""
你是求职助手的记忆入库规划器。请判断用户消息中哪些内容应该保存。

保存规则：
1. 学历、经验年限、技能、城市偏好、薪资底线、目标方向、明确不可接受条件，进入 profile_updates。
2. 项目描述、经历叙述、成果材料、HR 对话、较长背景材料，进入 long_texts。
3. 不确定或模糊内容不要硬写入结构化字段，可以保存为 long_texts。
4. 只返回 JSON，不要返回 Markdown。

当前候选人 ID：{candidate.id}
当前候选人姓名：{candidate.name}

JSON 格式：
{{
  "reply": "给用户的简短回复",
  "profile_updates": {{
    "status": null,
    "education": null,
    "experience_years": null,
    "skills": {{}},
    "preferred_cities": [],
    "salary_floor_k": null,
    "expected_salary_k": null,
    "target_directions": [],
    "unacceptable": []
  }},
  "long_texts": [
    {{"source_label": "conversation_note", "text": "需要保存的长文本"}}
  ]
}}

用户消息：
{message}
""".strip()


def decision_from_json(candidate_id: int, original_message: str, llm_text: str) -> ConversationIngestionDecision:
    """把 LLM 返回的 JSON 文本转换为保存决策。"""

    data = json.loads(extract_json_object(llm_text))
    profile_data = data.get("profile_updates") or {}
    patch = CandidateProfilePatch(
        status=empty_to_none(profile_data.get("status")),
        education=empty_to_none(profile_data.get("education")),
        experience_years=parse_float_or_none(profile_data.get("experience_years")),
        skills=clean_string_dict(profile_data.get("skills") or {}),
        preferred_cities=clean_string_list(profile_data.get("preferred_cities") or []),
        salary_floor_k=parse_int_or_none(profile_data.get("salary_floor_k")),
        expected_salary_k=parse_int_or_none(profile_data.get("expected_salary_k")),
        target_directions=clean_string_list(profile_data.get("target_directions") or []),
        unacceptable=clean_string_list(profile_data.get("unacceptable") or []),
    )
    long_texts = [
        LongTextInput(
            entity_type="conversation_message",
            entity_id=candidate_id,
            source_label="original_user_message",
            text=original_message,
        )
    ]
    for item in data.get("long_texts") or []:
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            continue
        long_texts.append(
            LongTextInput(
                entity_type="conversation_message",
                entity_id=candidate_id,
                source_label=str(item.get("source_label") or "llm_extracted_note"),
                text=str(item["text"]).strip(),
            )
        )
    return ConversationIngestionDecision(
        reply=str(data.get("reply") or "我已分析并保存这条资料。"),
        profile_updates=patch,
        long_texts=long_texts,
    )


def extract_json_object(text: str) -> str:
    """从 LLM 输出中提取 JSON 对象。"""

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise json.JSONDecodeError("No JSON object found", text, 0)
    return stripped[start : end + 1]


def rule_based_decision(candidate: CandidateProfile, message: str) -> ConversationIngestionDecision:
    """保守规则版保存决策。

    规则只提取非常明显的结构化事实；整条原文始终保存成长文本，避免漏掉信息。
    """

    patch = CandidateProfilePatch(
        education=extract_education(message),
        experience_years=extract_experience_years(message),
        skills=extract_skills_from_message(message),
        preferred_cities=extract_cities(message),
        target_directions=extract_target_directions(message),
        unacceptable=extract_unacceptable(message),
    )
    fields = patch_field_names(patch)
    reply = "已保存你的原始资料"
    if fields:
        reply += "，并自动更新：" + "、".join(fields)
    reply += "。"
    return ConversationIngestionDecision(
        reply=reply,
        profile_updates=patch,
        long_texts=[
            LongTextInput(
                entity_type="conversation_message",
                entity_id=candidate.id,
                source_label="original_user_message",
                text=message,
            )
        ],
    )


def extract_education(text: str) -> str | None:
    """从文本中提取最高学历。"""

    labels = [label for label in EDUCATION_ORDER if label not in {"不限", "学历不限"}]
    matched = [label for label in labels if label in text]
    if not matched:
        return None
    return max(matched, key=lambda label: EDUCATION_ORDER[label])


def extract_experience_years(text: str) -> float | None:
    """从文本中提取经验年限。"""

    match = re.search(r"(\d+(?:\.\d+)?)\s*年(?:工作)?(?:经验|经历)?", text)
    if not match:
        return None
    return float(match.group(1))


def extract_skills_from_message(text: str) -> dict[str, str]:
    """从消息中提取明确出现的技能词。"""

    lower_text = text.lower()
    return {
        skill: "待确认"
        for skill in KNOWN_SKILLS
        if skill.lower() in lower_text
    }


def extract_cities(text: str) -> list[str]:
    """提取常见城市偏好。"""

    cities = []
    for city in ("北京", "上海", "杭州", "深圳", "广州", "成都", "南京", "武汉", "西安", "苏州"):
        if city in text and any(keyword in text for keyword in ("城市", "地点", "base", "接受", "想去")):
            cities.append(city)
    return cities


def extract_target_directions(text: str) -> list[str]:
    """提取常见求职方向。"""

    directions = []
    if "agent" in text.lower() or "智能体" in text:
        directions.append("AI Agent 应用开发")
    if "后端" in text:
        directions.append("后端开发")
    return directions


def extract_unacceptable(text: str) -> list[str]:
    """提取明确不可接受条件。"""

    unacceptable = []
    for item in ("外包", "长期出差", "大小周", "单休"):
        if item in text and any(keyword in text for keyword in ("不接受", "不能接受", "不要", "排除")):
            unacceptable.append(item)
    return unacceptable


def patch_field_names(patch: CandidateProfilePatch) -> list[str]:
    """返回 patch 中包含实际内容的字段名。"""

    fields = []
    if patch.status:
        fields.append("status")
    if patch.education:
        fields.append("education")
    if patch.experience_years is not None:
        fields.append("experience_years")
    if patch.skills:
        fields.append("skills")
    if patch.preferred_cities:
        fields.append("preferred_cities")
    if patch.salary_floor_k is not None:
        fields.append("salary_floor_k")
    if patch.expected_salary_k is not None:
        fields.append("expected_salary_k")
    if patch.target_directions:
        fields.append("target_directions")
    if patch.unacceptable:
        fields.append("unacceptable")
    return fields


def empty_to_none(value: Any) -> str | None:
    """把空字符串/None 转成 None。"""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_float_or_none(value: Any) -> float | None:
    """解析可选浮点数。"""

    if value in (None, ""):
        return None
    return float(value)


def parse_int_or_none(value: Any) -> int | None:
    """解析可选整数。"""

    if value in (None, ""):
        return None
    return int(value)


def clean_string_list(value: Any) -> list[str]:
    """清理 LLM 返回的字符串列表。"""

    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def clean_string_dict(value: Any) -> dict[str, str]:
    """清理 LLM 返回的字符串字典。"""

    if not isinstance(value, dict):
        return {}
    return {
        str(key).strip(): str(item).strip() or "待确认"
        for key, item in value.items()
        if str(key).strip()
    }
