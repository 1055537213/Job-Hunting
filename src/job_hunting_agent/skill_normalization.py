"""候选人技能的规范化与同义写法合并规则。

模型返回和手工输入都可能出现大小写、空格、全半角或常见简称差异。这里提供
可解释的本地规则，作为写入候选人档案前的最后一道边界；不依赖模型猜测，也不对
没有明确映射关系的词做模糊合并，避免把不同技能错误地合并。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping

# 只收录行业中含义稳定的常见别名。未知名称仍按大小写、空格和常用连接符去重，
# 但不会用模糊匹配擅自认定为同一种技能。
SKILL_ALIASES: dict[str, tuple[str, ...]] = {
    "Python": ("Python", "Python 3", "Python3", "Py"),
    "JavaScript": ("JavaScript", "Java Script", "JS", "ECMAScript"),
    "TypeScript": ("TypeScript", "Type Script", "TS"),
    "Go": ("Go", "Golang", "GoLang"),
    "FastAPI": ("FastAPI", "Fast API"),
    "LangChain": ("LangChain", "Lang Chain"),
    "LangGraph": ("LangGraph", "Lang Graph"),
    "RAG": ("RAG", "Retrieval Augmented Generation", "检索增强", "检索增强生成"),
    "向量检索": (
        "向量检索",
        "向量搜索",
        "Vector",
        "Vector Search",
        "Vector Retrieval",
    ),
    "PostgreSQL": ("PostgreSQL", "Postgres", "PGSQL"),
    "Kubernetes": ("Kubernetes", "K8s"),
    "React": ("React", "React.js", "ReactJS"),
    "Vue": ("Vue", "Vue.js", "VueJS"),
    "Node.js": ("Node.js", "NodeJS", "Node"),
    "Agent": ("Agent", "AI Agent", "智能体"),
    "C#": ("C#", "C Sharp"),
}

UNAVAILABLE_SKILL_LEVELS = frozenset(
    {"明确不会", "不会", "不具备", "缺失", "没有", "无"}
)

SkillMention = tuple[str, str, int, int]


def skill_identity(value: object) -> str:
    """返回用于判定两条技能是否相同的稳定键。"""

    key = _comparison_key(value)
    return _ALIAS_TO_IDENTITY.get(key, key)


def canonical_skill_name(value: object) -> str:
    """返回已知技能的标准展示名，未知名称保留用户最先输入的写法。"""

    identity = skill_identity(value)
    return _CANONICAL_NAME_BY_IDENTITY.get(identity, _clean_skill_text(value))


def normalize_skill_mapping(skills: Mapping[str, object] | None) -> dict[str, str]:
    """合并一份技能字典中的大小写和常见别名重复项。

    同一批输入中首次出现的熟练度优先，避免后续重复项意外覆盖用户明确记录。
    """

    if not isinstance(skills, Mapping):
        return {}

    normalized: dict[str, str] = {}
    seen_identities: set[str] = set()
    for raw_name, raw_level in skills.items():
        identity = skill_identity(raw_name)
        if not identity or identity in seen_identities:
            continue
        name = canonical_skill_name(raw_name)
        if not name:
            continue
        normalized[name] = _clean_skill_level(raw_level)
        seen_identities.add(identity)
    return normalized


def merge_skill_mappings(
    existing: Mapping[str, object] | None,
    incoming: Mapping[str, object] | None,
) -> dict[str, str]:
    """把本轮技能更新合并到已有档案，按技能本质而非原始写法判重。"""

    merged = normalize_skill_mapping(existing)
    names_by_identity = {skill_identity(name): name for name in merged}
    if not isinstance(incoming, Mapping):
        return merged

    for raw_name, raw_level in incoming.items():
        identity = skill_identity(raw_name)
        if not identity:
            continue
        existing_name = names_by_identity.get(identity)
        if existing_name:
            # 本轮消息是较新的候选人陈述；只有熟练度变化时才覆盖已有值。
            merged[existing_name] = _clean_skill_level(raw_level)
            continue
        name = canonical_skill_name(raw_name)
        if not name:
            continue
        merged[name] = _clean_skill_level(raw_level)
        names_by_identity[identity] = name
    return merged


def find_skill_mentions(text: object, skills: Iterable[str]) -> list[SkillMention]:
    """按技能顺序查找独立提及，并返回技能名、命中文字和位置。

    英文简称使用 ASCII 单词边界，因此 ``JavaScript`` 不会额外命中 ``Java``，
    ``Django`` 也不会因为结尾字母而命中 ``Go``。中文技能词不强加英文边界。
    """

    normalized_text = unicodedata.normalize("NFKC", str(text or ""))
    mentions: list[SkillMention] = []
    for skill in skills:
        for alias in _aliases_for_skill(skill):
            match = re.search(_skill_mention_pattern(alias), normalized_text, re.IGNORECASE)
            if match is None:
                continue
            mentions.append((skill, match.group(0), match.start(), match.end()))
            break
    return mentions


def contains_skill_mention(text: object, skill: str) -> bool:
    """判断文本是否独立提及某项技能或它的已知别名。"""

    return bool(find_skill_mentions(text, [skill]))


def skill_level_is_available(level: object) -> bool:
    """返回技能熟练度是否代表候选人具备该技能。"""

    normalized = unicodedata.normalize("NFKC", str(level or "")).strip().casefold()
    return normalized not in UNAVAILABLE_SKILL_LEVELS


def _comparison_key(value: object) -> str:
    """统一全半角、大小写、空白和常用连接符，保留 C#、C++ 等语义符号。"""

    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return re.sub(r"[\s._/\\-]+", "", normalized)


def _clean_skill_text(value: object) -> str:
    """清理展示名称，但不改变未知技能的具体叫法。"""

    return " ".join(unicodedata.normalize("NFKC", str(value or "")).strip().split())


def _clean_skill_level(value: object) -> str:
    """清理熟练度并给空值保留现有默认语义。"""

    level = " ".join(str(value or "").strip().split())
    return level or "待确认"


def _aliases_for_skill(skill: str) -> tuple[str, ...]:
    identity = skill_identity(skill)
    canonical_name = _CANONICAL_NAME_BY_IDENTITY.get(identity)
    aliases = SKILL_ALIASES.get(canonical_name or "", ())
    ordered = [str(skill), *aliases]
    return tuple(dict.fromkeys(alias for alias in ordered if alias.strip()))


def _skill_mention_pattern(alias: str) -> str:
    normalized_alias = unicodedata.normalize("NFKC", alias).strip()
    escaped = re.escape(normalized_alias).replace(r"\ ", r"\s*")
    ascii_word_chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
    left_boundary = rf"(?<![{ascii_word_chars}])" if normalized_alias[:1] in ascii_word_chars else ""
    right_boundary = rf"(?![{ascii_word_chars}])" if normalized_alias[-1:] in ascii_word_chars else ""
    return left_boundary + escaped + right_boundary


def _build_alias_indexes() -> tuple[dict[str, str], dict[str, str]]:
    alias_to_identity: dict[str, str] = {}
    canonical_by_identity: dict[str, str] = {}
    for canonical_name, aliases in SKILL_ALIASES.items():
        identity = _comparison_key(canonical_name)
        canonical_by_identity[identity] = canonical_name
        for alias in aliases:
            alias_key = _comparison_key(alias)
            if alias_key:
                alias_to_identity[alias_key] = identity
    return alias_to_identity, canonical_by_identity


_ALIAS_TO_IDENTITY, _CANONICAL_NAME_BY_IDENTITY = _build_alias_indexes()
