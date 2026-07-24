"""职位原文标准化解析器。

当前版本是规则版解析器：它不访问 BOSS 页面，只处理候选人主动粘贴或导入的
职位原文。后续接入 LLM 时，最适合从这个模块扩展：规则先抽取确定字段，
LLM 再补充职责、要求、技能和不确定说明。
"""

from __future__ import annotations

import re
from datetime import datetime

from .models import ImportedJob


EDUCATION_ORDER = {
    # 学历等级来自决策地图 #7：学历是硬性条件，候选人学历不能低于职位要求。
    "学历不限": 0,
    "不限": 0,
    "高中": 1,
    "中专": 1,
    "大专": 2,
    "本科": 3,
    "硕士": 4,
    "博士": 5,
}

KNOWN_SKILLS = [
    # MVP 先用小词表演示技能抽取；后续可以改成“规则词表 + LLM 补充抽取”。
    "Python",
    "Java",
    "Go",
    "JavaScript",
    "TypeScript",
    "FastAPI",
    "Flask",
    "Django",
    "LangChain",
    "LangGraph",
    "RAG",
    "向量检索",
    "SQLite",
    "MySQL",
    "PostgreSQL",
    "Redis",
    "Docker",
    "Kubernetes",
    "React",
    "Vue",
    "Agent",
    "OpenAI",
]


def parse_job_text(raw_text: str, job_id: int = 0, source_url: str | None = None) -> ImportedJob:
    """把候选人导入的职位原文解析成 `ImportedJob`。

    解析目标不是做到百分百准确，而是先得到可比较字段，并明确标记字段置信度。
    """

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    compact_text = "\n".join(lines)

    title = lines[0] if lines else "未命名职位"
    salary_min, salary_max, salary_months, salary_unit = parse_salary(compact_text)
    exp_min, exp_max, exp_label = parse_experience(compact_text)
    education = parse_education(compact_text)
    city = parse_city(lines)
    company, industry, company_size = parse_company(lines, education)
    skills = extract_skills(compact_text)
    field_confidence = {
        # 置信度用于提醒后续匹配器：字段缺失或解析弱时不要装作确定。
        "title": 0.9 if title != "未命名职位" else 0.2,
        "salary": 0.9 if salary_max is not None else 0.2,
        "experience": 0.9 if exp_label else 0.2,
        "education": 0.9 if education else 0.2,
        "city": 0.8 if city else 0.2,
        "skills": 0.7 if skills else 0.2,
    }
    uncertainty_notes = [
        name
        for name, confidence in field_confidence.items()
        if confidence < 0.5
    ]

    return ImportedJob(
        id=job_id,
        raw_text=raw_text.strip(),
        source_url=source_url,
        title=title,
        city=city,
        salary_min_k=salary_min,
        salary_max_k=salary_max,
        salary_months=salary_months,
        salary_unit=salary_unit,
        experience_min_years=exp_min,
        experience_max_years=exp_max,
        experience_label=exp_label,
        education=education,
        company_name=company,
        industry=industry,
        company_size=company_size,
        skills=skills,
        description_text=compact_text,
        field_confidence=field_confidence,
        uncertainty_notes=uncertainty_notes,
    )


def parse_salary(text: str) -> tuple[int | None, int | None, int | None, str]:
    """解析薪资字段。

    月薪会拆成最低 K、最高 K、薪资月数；日薪和时薪保留原单位，不强行换算。
    """

    monthly = re.search(r"(\d+)\s*-\s*(\d+)\s*K(?:[·\.](\d+)薪)?", text, re.IGNORECASE)
    if monthly:
        return int(monthly.group(1)), int(monthly.group(2)), int(monthly.group(3) or 12), "month"

    daily = re.search(r"(\d+)\s*-\s*(\d+)\s*元\s*/?\s*天", text)
    if daily:
        return int(daily.group(1)), int(daily.group(2)), None, "day"

    hourly = re.search(r"(\d+)\s*-\s*(\d+)\s*元\s*/?\s*(?:时|小时)", text)
    if hourly:
        return int(hourly.group(1)), int(hourly.group(2)), None, "hour"

    return None, None, None, "unknown"


def parse_experience(text: str) -> tuple[float | None, float | None, str | None]:
    """解析经验要求，并转成可比较的年限上下限。"""

    if "经验不限" in text:
        return 0, None, "经验不限"
    if "在校/应届" in text or "应届" in text:
        return 0, 1, "在校/应届"

    range_match = re.search(r"(\d+)\s*-\s*(\d+)\s*年", text)
    if range_match:
        return float(range_match.group(1)), float(range_match.group(2)), range_match.group(0)

    min_match = re.search(r"(\d+)\s*年(?:以上)?", text)
    if min_match:
        return float(min_match.group(1)), None, min_match.group(0)

    return None, None, None


def parse_education(text: str) -> str | None:
    """解析学历要求。

    注意：只有解析出明确学历时，匹配器才会执行学历硬性淘汰。
    """

    for label in ("博士", "硕士", "本科", "大专", "中专", "高中", "学历不限"):
        if label in text:
            return label
    return None


def parse_city(lines: list[str]) -> str | None:
    """从职位前几行里猜测城市。

    BOSS 风格职位文本通常把城市放在薪资、经验、学历附近；这里先做保守猜测。
    """

    for line in lines[:8]:
        if re.fullmatch(r"[\u4e00-\u9fa5]{2,8}(?:[\u4e00-\u9fa5]{1,6})?", line):
            if line not in EDUCATION_ORDER and "年" not in line and "薪" not in line:
                return line[:2] if len(line) > 2 and line[:2] in line else line
    for city in ("北京", "上海", "杭州", "深圳", "广州", "成都", "南京", "武汉", "西安", "苏州"):
        if any(city in line for line in lines[:8]):
            return city
    return None


def parse_company(lines: list[str], education: str | None) -> tuple[str | None, str | None, str | None]:
    """解析公司名、行业和公司规模。

    这里是 MVP 规则：通常学历下一行是公司名，行业和规模通过固定形态识别。
    """

    company = None
    industry = None
    company_size = None
    education_index = next((index for index, line in enumerate(lines) if education and education in line), None)
    if education_index is not None and education_index + 1 < len(lines):
        company = lines[education_index + 1]
    for line in lines:
        if re.search(r"\d+-\d+人|\d+人以上", line):
            company_size = line
        elif line in {"人工智能", "计算机软件", "互联网", "企业服务", "电子商务", "互联网金融"}:
            industry = line
    return company, industry, company_size


def extract_skills(text: str) -> list[str]:
    """从职位全文中抽取技能词。

    这个函数只负责生成候选技能列表，不判断候选人是否具备；是否匹配由
    `matcher.py` 结合候选人档案处理。
    """

    found = []
    lower_text = text.lower()
    aliases = {
        "向量检索": ["向量检索", "vector"],
        "RAG": ["rag", "检索增强"],
        "Agent": ["agent", "智能体"],
    }
    for skill in KNOWN_SKILLS:
        terms = aliases.get(skill, [skill])
        if any(term.lower() in lower_text for term in terms):
            found.append(skill)
    return sorted(set(found), key=found.index)


def now_iso() -> str:
    """返回秒级 ISO 时间字符串，预留给后续记录导入时间。"""

    return datetime.now().isoformat(timespec="seconds")
