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

from .city_catalog import cities_in_text, nearby_cities, normalize_city_list
from .job_parser import EDUCATION_ORDER, KNOWN_SKILLS
from .llm import LLMClient
from .models import (
    CandidateProfile,
    CandidateProfilePatch,
    ConversationIngestionDecision,
    LongTextInput,
    sanitize_preference_weights,
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
            return decision_from_json(candidate, message, llm_text)
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
4. 用户明确的新首选城市覆盖旧首选城市；“也可以/可以考虑”的城市进入 acceptable_cities。
5. 用户说“附近/邻近城市也可以”时，只填写能够明确识别的城市；不要猜测未知城市。
6. 用户说“城市不限/任何城市都可以”时，将 clear_preferred_cities 和 clear_acceptable_cities 设为 true。
7. 用户明确说更看重某个维度时，preference_weights 只能使用 1.0、1.5、2.0。
8. 只返回 JSON，不要返回 Markdown。

当前候选人 ID：{candidate.id}
当前候选人姓名：{candidate.name}
当前首选城市：{candidate.preferred_cities}
当前其他可接受城市：{candidate.acceptable_cities}
当前偏好权重：{candidate.preference_weights}

JSON 格式：
{{
  "reply": "给用户的简短回复",
  "profile_updates": {{
    "status": null,
    "education": null,
    "experience_years": null,
    "skills": {{}},
    "preferred_cities": [],
    "acceptable_cities": [],
    "clear_preferred_cities": false,
    "clear_acceptable_cities": false,
    "preference_weights": {{}},
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


def decision_from_json(
    candidate: CandidateProfile,
    original_message: str,
    llm_text: str,
) -> ConversationIngestionDecision:
    """把 LLM 返回的 JSON 文本转换为保存决策。"""

    data = json.loads(extract_json_object(llm_text))
    profile_data = data.get("profile_updates") or {}
    rule_preferred, rule_acceptable, rule_clear = extract_city_preferences(candidate, original_message)
    rule_weights = extract_preference_weights(original_message)
    mentioned_cities = set(cities_in_text(original_message))
    llm_preferred = normalize_city_list(clean_string_list(profile_data.get("preferred_cities") or []))
    # 模型不能凭空写入城市：首选城市必须在用户原话中明确出现。
    preferred_cities = [city for city in llm_preferred if city in mentioned_cities] or rule_preferred
    allowed_acceptable = mentioned_cities | set(
        nearby_cities(rule_preferred or list(candidate.preferred_cities))
    )
    llm_acceptable = normalize_city_list(clean_string_list(profile_data.get("acceptable_cities") or []))
    acceptable_cities = [city for city in llm_acceptable if city in allowed_acceptable] or rule_acceptable
    # 清空属于高影响更新，只接受本地明确短语识别结果，不让模型单独推断。
    clear_preferred = rule_clear
    clear_acceptable = rule_clear
    raw_preference_weights = rule_weights or clean_number_dict(profile_data.get("preference_weights") or {})
    preference_weights = sanitize_preference_weights(raw_preference_weights)
    preference_updates = rule_weights or {
        key: preference_weights[key]
        for key in raw_preference_weights
        if key in preference_weights
        and preference_weights[key] != candidate.preference_weights.get(key, 1.0)
    }
    patch = CandidateProfilePatch(
        status=empty_to_none(profile_data.get("status")),
        education=empty_to_none(profile_data.get("education")),
        experience_years=parse_float_or_none(profile_data.get("experience_years")),
        skills=clean_string_dict(profile_data.get("skills") or {}),
        preferred_cities=preferred_cities,
        acceptable_cities=acceptable_cities,
        replace_preferred_cities=bool(preferred_cities),
        clear_preferred_cities=clear_preferred,
        clear_acceptable_cities=clear_acceptable,
        preference_weights=preference_updates,
        salary_floor_k=parse_int_or_none(profile_data.get("salary_floor_k")),
        expected_salary_k=parse_int_or_none(profile_data.get("expected_salary_k")),
        target_directions=clean_string_list(profile_data.get("target_directions") or []),
        unacceptable=clean_string_list(profile_data.get("unacceptable") or []),
    )
    long_texts = [
        LongTextInput(
            entity_type="conversation_message",
            entity_id=candidate.id,
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
                entity_id=candidate.id,
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

    preferred_cities, acceptable_cities, clear_cities = extract_city_preferences(candidate, message)
    preference_weights = extract_preference_weights(message)
    patch = CandidateProfilePatch(
        education=extract_education(message),
        experience_years=extract_experience_years(message),
        skills=extract_skills_from_message(message),
        preferred_cities=preferred_cities,
        acceptable_cities=acceptable_cities,
        replace_preferred_cities=bool(preferred_cities),
        clear_preferred_cities=clear_cities,
        clear_acceptable_cities=clear_cities,
        preference_weights=preference_weights,
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


def extract_city_preferences(
    candidate: CandidateProfile,
    text: str,
) -> tuple[list[str], list[str], bool]:
    """提取首选城市、其他可接受城市和明确清除意图。

    城市分类优先按同一句中的表达判断，避免“首选杭州，上海也可以”被全部
    归入同一类别。邻近城市只通过本地可靠关系目录扩展，目录未知时保持为空。
    """

    clear_markers = (
        "城市不限",
        "地点不限",
        "任何城市都可以",
        "哪个城市都可以",
        "哪里都可以",
        "不用管城市",
        "不限制城市",
        "全国都可以",
        "对城市没要求",
        "城市没有要求",
        "城市无所谓",
        "地点无所谓",
    )
    if any(marker in text for marker in clear_markers):
        return [], [], True

    preferred_markers = (
        "首选",
        "优先",
        "目标城市",
        "工作城市",
        "工作地点",
        "更想去",
        "想去",
        "想在",
        "希望去",
        "希望在",
    )
    acceptable_markers = (
        "也可以",
        "也能接受",
        "也接受",
        "可以考虑",
        "可接受",
        "都能接受",
    )
    nearby_markers = ("邻近城市", "附近城市", "周边城市", "周围城市")

    preferred: list[str] = []
    acceptable: list[str] = []
    clauses = [part.strip() for part in re.split(r"[，,。；;！？!?\n]+", text) if part.strip()]
    for clause in clauses:
        mentioned = cities_in_text(clause)
        if any(marker in clause for marker in nearby_markers):
            source = mentioned or preferred or list(candidate.preferred_cities)
            acceptable.extend(nearby_cities(source))
        elif any(marker in clause for marker in acceptable_markers):
            acceptable.extend(mentioned)
        elif any(marker in clause for marker in preferred_markers):
            preferred.extend(mentioned)

    preferred = normalize_city_list(preferred)
    acceptable = [
        city
        for city in normalize_city_list(acceptable)
        if city not in preferred and city not in candidate.preferred_cities
    ]
    return preferred, acceptable, False


def extract_preference_weights(text: str) -> dict[str, float]:
    """只从明确优先级表达中提取长期匹配权重。"""

    dimensions = {
        "city": ("城市", "地点"),
        "salary": ("薪资", "工资", "收入", "待遇"),
        "skills": ("技能", "技术", "技术栈"),
        "direction": ("方向", "岗位", "职位", "工作内容"),
        "experience": ("经验", "年限", "工作年限"),
    }
    # “首选薪资/首选城市”也是明确的最高优先级表达；普通的“想去杭州”
    # 仍只会改变城市事实，不会意外修改权重。
    strong_markers = ("最看重", "最重要", "最优先", "首要", "首选")
    medium_markers = ("更看重", "比较看重", "非常关注", "优先考虑", "优先")
    reset_markers = ("无所谓", "不在意", "不用管", "没有要求", "不重要")
    result: dict[str, float] = {}
    for clause in [part.strip() for part in re.split(r"[，,。；;！？!?\n]+", text) if part.strip()]:
        for dimension, keywords in dimensions.items():
            if not any(keyword in clause for keyword in keywords):
                continue
            if any(marker in clause for marker in strong_markers):
                result[dimension] = 2.0
            elif any(marker in clause for marker in medium_markers):
                result[dimension] = 1.5
            elif any(marker in clause for marker in reset_markers):
                result[dimension] = 1.0
    if not result:
        return {}
    # 这里必须返回“本轮明确提到的维度”，不能用完整默认字典填充其它维度，
    # 否则一次“最看重薪资”会把所有默认权重都误记成显式更新。
    normalized = sanitize_preference_weights(result)
    return {key: normalized[key] for key in result if key in normalized}


def extract_cities(text: str) -> list[str]:
    """兼容旧调用：只返回消息中明确作为首选表达的城市。"""

    placeholder = CandidateProfile(
        id=0,
        name="",
        status="",
        education="",
        experience_years=0,
        skills={},
        preferred_cities=[],
        salary_floor_k=None,
        expected_salary_k=None,
        target_directions=[],
        unacceptable=[],
        acceptable_cities=[],
    )
    preferred, _, _ = extract_city_preferences(placeholder, text)
    return preferred


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
    if patch.preferred_cities or patch.clear_preferred_cities:
        fields.append("preferred_cities")
    if patch.acceptable_cities or patch.clear_acceptable_cities:
        fields.append("acceptable_cities")
    if patch.salary_floor_k is not None:
        fields.append("salary_floor_k")
    if patch.expected_salary_k is not None:
        fields.append("expected_salary_k")
    if patch.target_directions:
        fields.append("target_directions")
    if patch.unacceptable:
        fields.append("unacceptable")
    if patch.preference_weights:
        fields.append("preference_weights")
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


def clean_number_dict(value: Any) -> dict[str, float]:
    """清理 LLM 返回的数值字典，后续仍由权重白名单再次校验。"""

    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for key, item in value.items():
        try:
            result[str(key).strip()] = float(item)
        except (TypeError, ValueError):
            continue
    return result
