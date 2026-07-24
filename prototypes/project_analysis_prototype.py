"""
PROTOTYPE - throwaway #11 local project analysis flow.

Run with:
    python ./prototypes/project_analysis_prototype.py <project_path>

This prototype answers:
    Can we read a candidate-provided local project with a minimal, privacy-aware
    scan and produce a confirmable project experience card?
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from pprint import pprint


MAX_FILE_BYTES = 120_000
MAX_TEXT_PER_FILE = 20_000
MAX_FILES = 80

SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".nuxt",
    "coverage",
    ".cache",
    "logs",
}

SENSITIVE_NAME_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\.env",
        r"secret",
        r"credential",
        r"password",
        r"passwd",
        r"token",
        r"private[_-]?key",
        r"id_rsa",
    )
]

SKIP_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".pyc",
    ".pyo",
    ".pem",
    ".p12",
    ".key",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".mp4",
    ".mov",
    ".avi",
    ".pdf",
    ".docx",
    ".xlsx",
}

IMPORTANT_FILE_NAMES = {
    "readme.md",
    "readme.txt",
    "pyproject.toml",
    "requirements.txt",
    "environment.yml",
    "environment.yaml",
    "package.json",
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "makefile",
}

SOURCE_SUFFIXES = {
    ".py",
    ".ipynb",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".vue",
    ".java",
    ".go",
    ".rs",
    ".cs",
    ".php",
    ".sql",
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
}

TECH_PATTERNS = {
    "Python": [r"\.py$", r"python"],
    "LangChain": [r"langchain"],
    "LangGraph": [r"langgraph"],
    "OpenAI API": [r"openai"],
    "FastAPI": [r"fastapi"],
    "Flask": [r"flask"],
    "Django": [r"django"],
    "Streamlit": [r"streamlit"],
    "SQLite": [r"sqlite"],
    "SQLAlchemy": [r"sqlalchemy"],
    "Chroma": [r"chromadb|chroma"],
    "FAISS": [r"faiss"],
    "Qdrant": [r"qdrant"],
    "React": [r"react"],
    "Vue": [r"vue"],
    "TypeScript": [r"typescript|\.ts$|\.tsx$"],
    "Node.js": [r"node|express|nestjs"],
    "Docker": [r"dockerfile|docker-compose|docker"],
    "Pytest": [r"pytest"],
    "RAG": [r"\brag\b|retriever|vector|embedding|向量|检索"],
    "Agent": [r"\bagent\b|tool_call|tools|智能体"],
}

FEATURE_PATTERNS = {
    "候选人档案/用户资料管理": [r"profile|candidate|resume|简历|候选人|档案"],
    "职位解析/标准化": [r"job|position|boss|zhipin|职位|岗位|招聘|标准化|解析"],
    "匹配排序/评分": [r"match|rank|score|recommend|匹配|排序|推荐|评分"],
    "向量检索/RAG": [r"rag|retriever|vector|embedding|向量|检索"],
    "Agent 流程/工具调用": [r"agent|tool_call|tools|chain|智能体|工具调用"],
    "接口/API 服务": [r"api|route|router|endpoint|fastapi|flask|controller"],
    "数据存储/数据库": [r"sqlite|database|sqlalchemy|model|schema|数据库"],
    "测试/质量验证": [r"test_|pytest|unittest|spec|测试"],
    "部署/容器化": [r"docker|compose|deploy|部署|容器"],
}


@dataclass
class FileEvidence:
    """单个被读取文件的证据记录。"""

    path: str
    reason: str
    size_bytes: int
    text: str


@dataclass
class ScanState:
    """项目扫描过程中的完整中间状态。

    原型会把状态打印出来，帮助我们判断读取边界和识别结果是否合理。
    """

    root: Path
    selected_files: list[FileEvidence] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    tech_evidence: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    feature_evidence: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    names: Counter[str] = field(default_factory=Counter)
    dependency_hints: set[str] = field(default_factory=set)


def should_skip_dir(path: Path) -> bool:
    """判断目录是否应该整体跳过。"""

    return path.name.lower() in SKIP_DIR_NAMES


def sensitive_name(path: Path) -> bool:
    """判断文件名是否可能包含敏感信息。"""

    text = path.name
    return any(pattern.search(text) for pattern in SENSITIVE_NAME_PATTERNS)


def should_select_file(path: Path) -> tuple[bool, str]:
    """决定是否读取某个文件，并返回选择/跳过原因。"""

    lower_name = path.name.lower()
    suffix = path.suffix.lower()

    if sensitive_name(path):
        return False, "sensitive_name"
    if suffix in SKIP_SUFFIXES:
        return False, "skipped_suffix"
    if lower_name in IMPORTANT_FILE_NAMES:
        return True, "important_file"
    if ".github" in [part.lower() for part in path.parts] and suffix in {".yml", ".yaml"}:
        return True, "workflow_config"
    if suffix in SOURCE_SUFFIXES:
        return True, "source_or_text"
    return False, "unsupported_type"


def read_text(path: Path) -> str:
    """安全读取文本文件，遇到大文件会交给上层跳过。"""

    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("large_file")

    if raw[:200].count(b"\x00") > 10:
        encodings = ("utf-16", "utf-16-le", "utf-16-be", "utf-8", "utf-8-sig", "gbk")
    else:
        encodings = ("utf-8", "utf-8-sig", "utf-16", "gbk")

    for encoding in encodings:
        try:
            return raw.decode(encoding, errors="strict")[:MAX_TEXT_PER_FILE]
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")[:MAX_TEXT_PER_FILE]


def walk_project(root: Path) -> ScanState:
    """遍历项目目录，收集可读取文件和跳过统计。"""

    state = ScanState(root=root)
    file_count = 0

    for path in root.rglob("*"):
        if any(should_skip_dir(parent) for parent in path.parents if parent != root):
            continue
        if path.is_dir():
            if should_skip_dir(path):
                state.skipped[f"dir:{path.name}"] += 1
            continue
        if not path.is_file():
            continue

        selected, reason = should_select_file(path)
        if not selected:
            state.skipped[reason] += 1
            continue

        try:
            size = path.stat().st_size
            text = read_text(path)
        except ValueError as exc:
            state.skipped[str(exc)] += 1
            continue
        except OSError:
            state.skipped["read_error"] += 1
            continue

        relative = str(path.relative_to(root))
        state.selected_files.append(
            FileEvidence(path=relative, reason=reason, size_bytes=size, text=text)
        )
        file_count += 1
        if file_count >= MAX_FILES:
            state.skipped["max_files_reached"] += 1
            break

    return state


def extract_dependency_hints(state: ScanState) -> None:
    """从 requirements/package/pyproject 等依赖配置中提取依赖名。"""

    for evidence in state.selected_files:
        name = Path(evidence.path).name.lower()
        text = evidence.text

        if name == "package.json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            for section in ("dependencies", "devDependencies"):
                for dep in data.get(section, {}):
                    state.dependency_hints.add(dep)

        if name == "requirements.txt":
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    state.dependency_hints.add(re.split(r"[<>=~! ]", line)[0])

        if name == "pyproject.toml":
            for match in re.finditer(r'"([^"]+)"', text):
                package = re.split(r"[<>=~! ]", match.group(1))[0]
                if package and len(package) < 60:
                    state.dependency_hints.add(package)


def extract_names(state: ScanState) -> None:
    """抽取函数名、类名等代码符号，作为项目结构线索。"""

    for evidence in state.selected_files:
        for pattern in (r"^\s*def\s+([a-zA-Z_]\w*)", r"^\s*class\s+([a-zA-Z_]\w*)"):
            for match in re.finditer(pattern, evidence.text, re.MULTILINE):
                state.names[match.group(1)] += 1
        for match in re.finditer(r"function\s+([a-zA-Z_]\w*)", evidence.text):
            state.names[match.group(1)] += 1


def detection_haystack(evidence: FileEvidence) -> str:
    """构造技术/功能识别文本，减少源码关键词假阳性。"""

    path = Path(evidence.path)
    suffix = path.suffix.lower()
    lower_name = path.name.lower()

    if lower_name in IMPORTANT_FILE_NAMES or suffix in {".md", ".toml", ".yaml", ".yml", ".json"}:
        return f"{evidence.path}\n{evidence.text[:MAX_TEXT_PER_FILE]}"

    interesting_lines = []
    for line in evidence.text.splitlines():
        stripped = line.strip()
        if suffix == ".py" and (
            stripped.startswith(("import ", "from ", "class ", "def ", "@"))
        ):
            interesting_lines.append(stripped)
        elif suffix in {".js", ".jsx", ".ts", ".tsx", ".vue"} and (
            stripped.startswith(("import ", "export ", "const ", "function ", "class "))
            or "require(" in stripped
        ):
            interesting_lines.append(stripped)
        elif suffix in {".java", ".go", ".rs", ".cs", ".php"} and (
            stripped.startswith(("import ", "use ", "package ", "class ", "func "))
        ):
            interesting_lines.append(stripped)

    return f"{evidence.path}\n" + "\n".join(interesting_lines[:200])


def detect_patterns(state: ScanState) -> None:
    """根据规则词表识别技术栈和核心功能线索。"""

    all_dependency_text = " ".join(sorted(state.dependency_hints)).lower()

    for evidence in state.selected_files:
        haystack = detection_haystack(evidence).lower()
        for tech, patterns in TECH_PATTERNS.items():
            if any(re.search(pattern, haystack, re.IGNORECASE) for pattern in patterns):
                state.tech_evidence[tech].add(evidence.path)
        for feature, patterns in FEATURE_PATTERNS.items():
            if any(re.search(pattern, haystack, re.IGNORECASE) for pattern in patterns):
                state.feature_evidence[feature].add(evidence.path)

    for tech, patterns in TECH_PATTERNS.items():
        if any(re.search(pattern, all_dependency_text, re.IGNORECASE) for pattern in patterns):
            state.tech_evidence[tech].add("dependency_config")


def make_card(state: ScanState) -> dict[str, object]:
    """把扫描状态转成待确认项目经历卡片。"""

    techs = sorted(state.tech_evidence)
    features = sorted(state.feature_evidence)
    top_names = [name for name, _ in state.names.most_common(12)]

    responsibilities = []
    if "候选人档案/用户资料管理" in features:
        responsibilities.append("可能负责候选人档案或简历资料建模")
    if "职位解析/标准化" in features:
        responsibilities.append("可能负责职位文本解析、字段标准化或导入流程")
    if "匹配排序/评分" in features:
        responsibilities.append("可能负责职位匹配、排序或推荐解释逻辑")
    if "向量检索/RAG" in features:
        responsibilities.append("可能负责向量检索、RAG 或长文本语义检索流程")
    if "接口/API 服务" in features:
        responsibilities.append("可能负责接口/API 服务设计")
    if "部署/容器化" in features:
        responsibilities.append("可能负责部署配置或容器化")
    if not responsibilities:
        responsibilities.append("需要候选人补充说明本人职责，当前仅识别到项目文件结构和技术线索")

    highlights = []
    if {"Agent", "LangChain"} & set(techs):
        highlights.append("项目包含 Agent 或 LangChain 相关实现线索")
    if "RAG" in techs or "向量检索/RAG" in features:
        highlights.append("项目包含向量检索/RAG 相关线索")
    if "测试/质量验证" in features:
        highlights.append("项目包含测试或质量验证线索")
    if "部署/容器化" in features:
        highlights.append("项目包含部署或容器化配置线索")
    if not highlights:
        highlights.append("当前亮点需要候选人结合业务目标和成果补充确认")

    return {
        "card_type": "待确认项目经历卡片",
        "project_name": state.root.name,
        "read_boundary": {
            "selected_files": len(state.selected_files),
            "skipped_summary": dict(state.skipped),
            "max_files": MAX_FILES,
            "max_file_bytes": MAX_FILE_BYTES,
        },
        "detected_tech_stack": [
            {
                "name": tech,
                "evidence": sorted(list(paths))[:5],
                "status": "待候选人确认",
            }
            for tech, paths in sorted(state.tech_evidence.items())
        ],
        "detected_core_features": [
            {
                "name": feature,
                "evidence": sorted(list(paths))[:5],
                "status": "待候选人确认",
            }
            for feature, paths in sorted(state.feature_evidence.items())
        ],
        "notable_code_symbols": top_names,
        "responsibility_draft": responsibilities,
        "highlight_draft": highlights,
        "resume_expression_draft": [
            "基于项目证据材料，整理技术栈、核心功能和候选人职责草稿，用于后续候选人确认。",
            "当前原型不自动写入候选人档案，也不把代码存在直接等同于候选人本人贡献。",
        ],
        "questions_for_candidate": [
            "这些技术栈中哪些是你本人实际使用或负责的？熟练度分别是什么？",
            "项目中你负责的模块、代码或设计边界是什么？",
            "项目是否有可运行 Demo、部署地址、测试数据或评估结果？",
            "是否有可以确认的成果数字，例如处理数据量、响应时间、准确率或节省时间？",
            "哪些内容可以写进简历，哪些只是学习或参考材料？",
        ],
    }


def main() -> None:
    """运行 #11 本地项目分析原型。"""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "project_path",
        nargs="?",
        default=".",
        help="Candidate-provided project directory to analyze.",
    )
    args = parser.parse_args()

    root = Path(args.project_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Project directory not found: {root}")

    print("PROTOTYPE #11：本地项目分析")
    print(f"目标目录：{root}")

    state = walk_project(root)
    print("\n--- 读取边界状态 ---")
    pprint(
        {
            "selected_files": [
                {
                    "path": evidence.path,
                    "reason": evidence.reason,
                    "size_bytes": evidence.size_bytes,
                }
                for evidence in state.selected_files[:30]
            ],
            "selected_file_count": len(state.selected_files),
            "skipped": dict(state.skipped),
        },
        sort_dicts=False,
    )

    extract_dependency_hints(state)
    extract_names(state)
    detect_patterns(state)

    print("\n--- 识别状态 ---")
    pprint(
        {
            "dependency_hints": sorted(state.dependency_hints)[:40],
            "tech_stack": {
                key: sorted(value)[:5] for key, value in state.tech_evidence.items()
            },
            "features": {
                key: sorted(value)[:5] for key, value in state.feature_evidence.items()
            },
            "notable_code_symbols": state.names.most_common(12),
        },
        sort_dicts=False,
    )

    print("\n--- 待确认项目经历卡片 ---")
    pprint(make_card(state), sort_dicts=False)

    print("\n原型结论：")
    print("项目分析应该输出待确认卡片，而不是直接写入候选人档案。")
    print("读取策略需要保留 selected/skipped 状态，让候选人知道系统读了什么、跳过了什么。")


if __name__ == "__main__":
    main()
