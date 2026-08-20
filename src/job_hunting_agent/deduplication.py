"""账号内精确去重所需的规范化和内容指纹工具。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from .city_catalog import normalize_city_list
from .models import (
    CandidateProfileInput,
    ProjectExperienceCard,
    sanitize_preference_weights,
)
from .skill_normalization import normalize_skill_mapping


class DuplicateResourceError(ValueError):
    """表示同一账号中已经存在相同的用户资源。"""

    def __init__(
        self,
        resource_label: str,
        *,
        message: str | None = None,
        existing_id: int | None = None,
    ) -> None:
        self.resource_label = resource_label
        self.existing_id = existing_id
        super().__init__(message or f"相同的{resource_label}已存在，未重复保存。")


def content_fingerprint(payload: object) -> str:
    """为已规范化内容生成稳定的 SHA-256 指纹，不把原文暴露到错误信息中。"""

    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def normalize_import_text(value: str) -> str:
    """忽略复制粘贴带来的换行符和行尾空白差异，保留正文内容。"""

    lines = str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized = [line.rstrip() for line in lines]
    while normalized and not normalized[0]:
        normalized.pop(0)
    while normalized and not normalized[-1]:
        normalized.pop()
    return "\n".join(normalized)


def candidate_profile_content_fingerprint(profile: CandidateProfileInput) -> str:
    """把候选人表单的所有结构化事实规范化为一个账号内唯一的内容指纹。"""

    preferred_cities = normalize_city_list(profile.preferred_cities)
    acceptable_cities = [
        city
        for city in normalize_city_list(profile.acceptable_cities)
        if city not in preferred_cities
    ]
    payload = {
        "name": _normalize_scalar(profile.name),
        "status": _normalize_scalar(profile.status),
        "education": _normalize_scalar(profile.education),
        "experience_years": float(profile.experience_years),
        "salary_floor_k": profile.salary_floor_k,
        "expected_salary_k": profile.expected_salary_k,
        # 技能的大小写和常见别名不应让同一档案绕过内容去重。
        "skills": normalize_skill_mapping(profile.skills),
        # 多选城市和方向在表单中没有先后语义；排序后避免仅因点击顺序不同而重复创建。
        "preferred_cities": _normalized_values(preferred_cities),
        "acceptable_cities": _normalized_values(acceptable_cities),
        "preference_weights": sanitize_preference_weights(profile.preference_weights),
        "target_directions": _normalized_values(profile.target_directions),
        "unacceptable": _normalized_values(profile.unacceptable),
    }
    return content_fingerprint(payload)


def job_text_content_fingerprint(raw_text: str) -> str:
    """职位去重以候选人主动导入的可见原文为准。"""

    return content_fingerprint({"raw_text": normalize_import_text(raw_text)})


def github_project_content_fingerprint(repository_url: str) -> str:
    """公开 GitHub 仓库以规范化 URL 为唯一来源，避免重复排队分析。"""

    return content_fingerprint(
        {
            "source_type": "github_public_repository",
            "source_url": _normalize_repository_url(repository_url),
        }
    )


def project_card_content_fingerprint(card: ProjectExperienceCard) -> str:
    """为待确认项目卡片生成内容指纹；GitHub 项目优先按仓库来源防重。"""

    if card.source_type == "github_public_repository" and card.source_url:
        return github_project_content_fingerprint(card.source_url)
    return content_fingerprint(_normalize_json_value(asdict(card)))


def is_unique_constraint_violation(error: Exception, constraint_name: str) -> bool:
    """识别 PostgreSQL 的唯一约束冲突，同时保持仓储层对驱动异常类型的宽容。"""

    original = getattr(error, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(error, "sqlstate", None)
    if sqlstate == "23505":
        return True
    error_text = " ".join(
        str(item)
        for item in (error, original)
        if item is not None
    ).lower()
    return constraint_name.lower() in error_text and "unique" in error_text


def _normalize_scalar(value: object) -> str:
    return str(value or "").strip()


def _normalized_values(values: list[str]) -> list[str]:
    normalized = {_normalize_scalar(value) for value in values}
    return sorted(value for value in normalized if value)


def _normalize_repository_url(value: str) -> str:
    return str(value or "").strip().rstrip("/").casefold()


def _normalize_json_value(value: Any) -> Any:
    """递归整理本地项目卡片，忽略字符串两端空白而不改变列表顺序。"""

    if isinstance(value, dict):
        return {str(key): _normalize_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    if isinstance(value, str):
        return _normalize_scalar(value)
    return value
