"""职位原文标准化解析器。

当前版本是规则版解析器：它不访问 BOSS 页面，只处理候选人主动粘贴或导入的
职位原文。后续接入 LLM 时，最适合从这个模块扩展：规则先抽取确定字段，
LLM 再补充职责、要求、技能和不确定说明。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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

JOB_TITLE_KEYWORDS = (
    # 常见职位标题关键词；用于判断首行是否像“岗位名称”，而不是项目日志或普通聊天。
    "工程师",
    "开发",
    "实习",
    "测试",
    "算法",
    "数据",
    "产品",
    "运营",
    "设计",
    "架构",
    "前端",
    "后端",
    "全栈",
    "客户端",
    "服务端",
    "研发",
    "助理",
    "专员",
    "顾问",
    "经理",
    "Java",
    "Python",
    "AI",
    "Agent",
)

JOB_STRUCTURE_KEYWORDS = (
    # 招聘文本里常见的结构化字段或段落标题。
    "职位描述",
    "岗位职责",
    "工作职责",
    "任职要求",
    "岗位要求",
    "职位要求",
    "专业要求",
    "职位类别",
    "职位性质",
    "工作年限",
    "工作地点",
    "薪资",
    "招聘",
    "立即投递",
)

JOB_DESCRIPTION_KEYWORDS = (
    # 职责/要求段落中的动作词；只作为辅助信号，不能单独证明是职位。
    "负责",
    "参与",
    "完成",
    "熟悉",
    "掌握",
    "具备",
    "优先",
    "要求",
    "职责",
    "任职",
    "工作内容",
)

KNOWN_CITIES = (
    "北京",
    "上海",
    "杭州",
    "深圳",
    "广州",
    "成都",
    "南京",
    "武汉",
    "西安",
    "苏州",
    "长沙",
    "天津",
    "重庆",
    "厦门",
    "合肥",
    "郑州",
    "青岛",
    "宁波",
)


class InvalidJobTextError(ValueError):
    """职位文本审核失败。

    把错误独立成类型，是为了让 Web 层可以返回 400，而不是把用户输入错误当成服务异常。
    """


@dataclass(frozen=True)
class JobTextValidationResult:
    """职位文本审核结果。

    `score` 是启发式分数，只用于判断“是否像招聘职位”，不是职位匹配分数。
    """

    is_valid: bool
    score: int
    matched_signals: list[str]
    missing_suggestions: list[str]

    def error_message(self) -> str:
        """把审核失败原因整理成可以直接展示给用户的中文错误。"""

        suggestions = "、".join(self.missing_suggestions)
        matched = "、".join(self.matched_signals) or "无明显招聘信号"
        return (
            "输入内容不像一段完整的招聘职位信息，已拒绝保存。"
            f"请补充：{suggestions}。"
            f"当前识别到的信号：{matched}。"
        )


def parse_job_text(raw_text: str, job_id: int = 0, source_url: str | None = None) -> ImportedJob:
    """把候选人导入的职位原文解析成 `ImportedJob`。

    解析目标不是做到百分百准确，而是先得到可比较字段，并明确标记字段置信度。
    """

    ensure_valid_job_text(raw_text)

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


def ensure_valid_job_text(raw_text: str) -> JobTextValidationResult:
    """审核用户粘贴的内容是否像招聘职位信息。

    这里采用保守规则：至少要有一个像职位标题的首行，并且同时出现多项招聘文本信号。
    这样可以挡住普通聊天、项目更新日志、单个数字、代码片段等误导入内容。
    """

    result = validate_job_text(raw_text)
    if not result.is_valid:
        raise InvalidJobTextError(result.error_message())
    return result


def validate_job_text(raw_text: str) -> JobTextValidationResult:
    """返回职位文本审核结果，不做数据库写入。"""

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    compact_text = "\n".join(lines)
    matched_signals: list[str] = []
    missing_suggestions: list[str] = []

    if len(compact_text) < 20:
        return JobTextValidationResult(
            is_valid=False,
            score=0,
            matched_signals=[],
            missing_suggestions=["职位名称", "职位描述或任职要求", "薪资/地点/经验/学历等字段"],
        )

    first_line = lines[0] if lines else ""
    title_like = is_plausible_job_title(first_line)
    if title_like:
        matched_signals.append("职位名称")
    else:
        missing_suggestions.append("清晰的职位名称")

    salary_min, salary_max, _, _ = parse_salary(compact_text)
    has_salary = salary_min is not None and salary_max is not None
    if has_salary:
        matched_signals.append("薪资")

    _, _, experience_label = parse_experience(compact_text)
    has_experience = experience_label is not None or "工作年限" in compact_text
    if has_experience:
        matched_signals.append("经验要求")

    has_education = parse_education(compact_text) is not None or "学历" in compact_text
    if has_education:
        matched_signals.append("学历要求")

    has_location = "工作地点" in compact_text or any(city in compact_text for city in KNOWN_CITIES)
    if has_location:
        matched_signals.append("工作地点")

    has_structure = any(keyword in compact_text for keyword in JOB_STRUCTURE_KEYWORDS)
    if has_structure:
        matched_signals.append("招聘字段")

    has_description = any(keyword in compact_text for keyword in JOB_DESCRIPTION_KEYWORDS)
    if has_description:
        matched_signals.append("职责/要求描述")

    has_company_context = bool(re.search(r"\d+-\d+人|\d+人以上|公司|企业|集团", compact_text))
    if has_company_context:
        matched_signals.append("公司信息")

    score = 0
    score += 2 if title_like else 0
    score += 2 if has_structure else 0
    score += 1 if has_salary else 0
    score += 1 if has_experience else 0
    score += 1 if has_education else 0
    score += 1 if has_location else 0
    score += 1 if has_description else 0
    score += 1 if has_company_context else 0

    if not any([has_salary, has_experience, has_education, has_location, has_structure]):
        missing_suggestions.append("薪资/地点/经验/学历/职位字段中的至少两项")
    if not has_description and not has_structure:
        missing_suggestions.append("职位描述、岗位职责或任职要求")

    # 需要“像职位标题”并且至少有若干招聘结构信号；否则项目日志里出现技术词也不能入库。
    is_valid = title_like and score >= 4 and len(matched_signals) >= 3
    return JobTextValidationResult(
        is_valid=is_valid,
        score=score,
        matched_signals=matched_signals,
        missing_suggestions=missing_suggestions or ["更完整的职位原文"],
    )


def is_plausible_job_title(line: str) -> bool:
    """判断首行是否像职位名称。

    首行以项目符号开头、过短、过长或更像说明句时，都不认为是职位标题。
    """

    normalized = line.strip()
    if not normalized or len(normalized) < 2 or len(normalized) > 80:
        return False
    if re.match(r"^[-*+•·]\s*", normalized):
        return False
    if re.fullmatch(r"\d+|https?://\S+", normalized):
        return False
    if normalized.endswith(("。", "；", ";")) and not any(keyword in normalized for keyword in JOB_TITLE_KEYWORDS):
        return False
    return any(keyword.lower() in normalized.lower() for keyword in JOB_TITLE_KEYWORDS)


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
