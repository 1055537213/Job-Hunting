"""本地项目分析器。

候选人可以把本地项目目录交给系统分析，但分析结果只能生成
“待确认项目经历卡片”，不能自动写入候选人档案。这个模块只做最小必要读取：
跳过密钥、数据库、日志、依赖目录、构建产物和大文件。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

from .models import ProjectExperienceCard


MAX_FILE_BYTES = 120_000
MAX_FILES = 80

# 这些目录通常不是候选人手写业务代码，或可能包含大量生成内容。
SKIP_DIRS = {
    ".git",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "dist",
    "build",
    ".cache",
    "logs",
}
# 这些后缀要么是敏感/二进制/生成文件，要么不适合在 MVP 里直接读取。
SKIP_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pyc", ".pem", ".key", ".zip", ".png", ".jpg", ".pdf"}
SOURCE_SUFFIXES = {".py", ".ipynb", ".js", ".ts", ".md", ".txt", ".toml", ".yaml", ".yml", ".json"}
IMPORTANT_NAMES = {"readme.md", "requirements.txt", "pyproject.toml", "package.json", "dockerfile", "docker-compose.yml"}
SENSITIVE_PATTERNS = [re.compile(pattern, re.I) for pattern in (r"^\.env", r"secret", r"credential", r"password", r"token", r"private[_-]?key")]

# 技术栈识别先用规则词表，后续可以让 LLM 基于 read_files 和证据片段生成更稳的卡片。
TECH_PATTERNS = {
    "Python": [r"\.py$", r"python"],
    "LangChain": [r"langchain"],
    "LangGraph": [r"langgraph"],
    "FastAPI": [r"fastapi"],
    "SQLite": [r"sqlite"],
    "RAG": [r"\brag\b|向量|检索|embedding|vector"],
    "Agent": [r"\bagent\b|智能体|tool"],
    "Docker": [r"docker"],
}

# 核心功能识别用于生成职责草稿；每一项都必须等待候选人确认。
FEATURE_PATTERNS = {
    "候选人档案/资料管理": [r"profile|candidate|resume|简历|候选人|档案"],
    "职位解析/标准化": [r"job|position|boss|zhipin|职位|岗位|标准化|解析"],
    "匹配排序/评分": [r"match|rank|score|recommend|匹配|排序|推荐|评分"],
    "向量检索/RAG": [r"rag|retriever|vector|embedding|向量|检索"],
    "Agent 流程/工具调用": [r"agent|tool_call|tools|chain|智能体|工具调用"],
    "接口/API 服务": [r"api|route|router|endpoint|fastapi|flask"],
    "测试/质量验证": [r"test_|pytest|unittest|测试"],
    "部署/容器化": [r"docker|compose|deploy|部署|容器"],
}


def analyze_project(project_path: str | Path) -> ProjectExperienceCard:
    """扫描本地项目目录，返回待确认项目经历卡片。"""

    root = Path(project_path).resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(root)

    selected: list[tuple[Path, str]] = []
    skipped: Counter[str] = Counter()
    for path in root.rglob("*"):
        # 如果父目录已经属于跳过范围，子文件也不再检查，避免读 node_modules 等目录。
        if any(part.lower() in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if path.is_dir():
            if path.name.lower() in SKIP_DIRS:
                skipped[f"dir:{path.name}"] += 1
            continue
        if not path.is_file():
            continue
        if is_sensitive(path):
            skipped["sensitive_name"] += 1
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            skipped["skipped_suffix"] += 1
            continue
        if path.name.lower() not in IMPORTANT_NAMES and path.suffix.lower() not in SOURCE_SUFFIXES:
            skipped["unsupported_type"] += 1
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            skipped["large_file"] += 1
            continue
        selected.append((path, read_text(path)))
        if len(selected) >= MAX_FILES:
            skipped["max_files_reached"] += 1
            break

    tech_evidence: dict[str, set[str]] = defaultdict(set)
    feature_evidence: dict[str, set[str]] = defaultdict(set)
    symbol_names: Counter[str] = Counter()

    for path, text in selected:
        relative = str(path.relative_to(root))
        # detection_haystack 会收窄源码文件里的识别范围，避免把工具自身的关键词表
        # 误判成项目实际使用的技术。
        haystack = detection_haystack(path, text).lower()
        for tech, patterns in TECH_PATTERNS.items():
            if any(re.search(pattern, haystack, re.I) for pattern in patterns):
                tech_evidence[tech].add(relative)
        for feature, patterns in FEATURE_PATTERNS.items():
            if any(re.search(pattern, haystack, re.I) for pattern in patterns):
                feature_evidence[feature].add(relative)
        for match in re.finditer(r"^\s*(?:def|class)\s+([a-zA-Z_]\w*)", text, re.MULTILINE):
            symbol_names[match.group(1)] += 1

    techs = sorted(tech_evidence)
    features = sorted(feature_evidence)
    responsibilities = responsibility_draft(features)

    return ProjectExperienceCard(
        card_type="待确认项目经历卡片",
        project_name=root.name,
        read_files=[str(path.relative_to(root)) for path, _ in selected],
        skipped_summary=dict(skipped),
        detected_tech_stack=techs,
        detected_core_features=features,
        responsibility_draft=responsibilities,
        highlight_draft=highlight_draft(techs, features),
        resume_expression_draft=[
            "基于项目证据材料整理技术栈、核心功能和职责草稿，等待候选人确认。",
            "代码中存在某项能力线索，不等于候选人本人负责过该能力。",
        ],
        questions_for_candidate=[
            # 这些问题是真实性边界的一部分：系统发现线索，候选人确认贡献。
            "这些技术栈中哪些是你本人实际使用或负责的？熟练度分别是什么？",
            "项目中你负责的模块、代码或设计边界是什么？",
            "项目是否有可运行 Demo、部署地址、测试数据或评估结果？",
            "是否有可以确认的成果数字，例如处理数据量、响应时间、准确率或节省时间？",
        ],
    )


def is_sensitive(path: Path) -> bool:
    """判断文件名是否像密钥、凭证或环境配置。"""

    return any(pattern.search(path.name) for pattern in SENSITIVE_PATTERNS)


def read_text(path: Path) -> str:
    """读取文本文件，并兼容 UTF-8、UTF-16、GBK 等常见编码。"""

    raw = path.read_bytes()
    encodings = ("utf-16", "utf-16-le", "utf-16-be", "utf-8", "utf-8-sig", "gbk") if raw[:200].count(b"\x00") > 10 else ("utf-8", "utf-8-sig", "utf-16", "gbk")
    for encoding in encodings:
        try:
            return raw.decode(encoding, errors="strict")[:20_000]
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")[:20_000]


def detection_haystack(path: Path, text: str) -> str:
    """构造用于识别技术栈和功能的文本。

    文档和配置文件可以全文参与识别；源码文件只保留 import/class/def 等结构性行，
    这样可以减少“源码里出现某个词，但项目并未使用该技术”的假阳性。
    """

    suffix = path.suffix.lower()
    if path.name.lower() in IMPORTANT_NAMES or suffix in {".md", ".toml", ".yaml", ".yml", ".json"}:
        return f"{path}\n{text}"
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if suffix == ".py" and stripped.startswith(("import ", "from ", "class ", "def ", "@")):
            lines.append(stripped)
        elif suffix in {".js", ".ts"} and (stripped.startswith(("import ", "export ", "function ", "class ")) or "require(" in stripped):
            lines.append(stripped)
    return f"{path}\n" + "\n".join(lines[:200])


def responsibility_draft(features: list[str]) -> list[str]:
    """把识别到的功能线索转成待确认职责草稿。"""

    mapping = {
        "候选人档案/资料管理": "可能负责候选人档案或简历资料建模",
        "职位解析/标准化": "可能负责职位文本解析、字段标准化或导入流程",
        "匹配排序/评分": "可能负责职位匹配、排序或推荐解释逻辑",
        "向量检索/RAG": "可能负责向量检索、RAG 或长文本语义检索流程",
        "Agent 流程/工具调用": "可能负责 Agent 流程或工具调用设计",
        "接口/API 服务": "可能负责接口/API 服务设计",
    }
    return [mapping[feature] for feature in features if feature in mapping] or ["需要候选人补充本人职责边界"]


def highlight_draft(techs: list[str], features: list[str]) -> list[str]:
    """把技术栈和功能线索转成待确认项目亮点。"""

    highlights = []
    if "Agent" in techs or "LangChain" in techs:
        highlights.append("项目包含 Agent 或 LangChain 相关实现线索")
    if "RAG" in techs or "向量检索/RAG" in features:
        highlights.append("项目包含向量检索/RAG 相关线索")
    if "测试/质量验证" in features:
        highlights.append("项目包含测试或质量验证线索")
    return highlights or ["当前亮点需要候选人结合业务目标和成果补充确认"]
