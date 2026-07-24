"""SQLite 存储层。

这个模块是当前 MVP 的事实源：

- `candidate_profiles` 保存候选人的结构化事实和偏好。
- `jobs` 保存职位原文及其标准化字段。
- `long_texts` 先作为长文本检索的占位表，后续可以替换或同步到向量库。

注意：这里不接触 BOSS 账号，也不自动抓取职位，只保存候选人主动提供的数据。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .job_parser import parse_job_text
from .models import (
    CandidateProfile,
    CandidateProfileInput,
    CandidateProfilePatch,
    ImportedJob,
    LongTextRecord,
    ProjectExperienceCard,
    ProjectExperienceRecord,
    ResumeDraft,
    ResumeDraftRecord,
)


class SQLiteStore:
    """封装所有 SQLite 读写。

    业务层通过这个类保存和读取实体，不需要关心表结构和 JSON 序列化细节。
    """

    def __init__(self, db_path: str | Path):
        """记录数据库路径；真正连接会在每次操作时创建。"""

        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        """创建 SQLite 连接，并让查询结果可以像字典一样按列名读取。"""

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        """创建 MVP 所需的数据表。

        当前先把 list/dict 字段保存成 JSON 文本，这是教学版里最直观的做法；
        以后如果查询需求变复杂，再拆成独立关系表。
        """

        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS candidate_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    education TEXT NOT NULL,
                    experience_years REAL NOT NULL,
                    salary_floor_k INTEGER,
                    expected_salary_k INTEGER,
                    skills_json TEXT NOT NULL,
                    preferred_cities_json TEXT NOT NULL,
                    target_directions_json TEXT NOT NULL,
                    unacceptable_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_text TEXT NOT NULL,
                    source_url TEXT,
                    title TEXT NOT NULL,
                    city TEXT,
                    salary_min_k INTEGER,
                    salary_max_k INTEGER,
                    salary_months INTEGER,
                    salary_unit TEXT NOT NULL,
                    experience_min_years REAL,
                    experience_max_years REAL,
                    experience_label TEXT,
                    education TEXT,
                    company_name TEXT,
                    industry TEXT,
                    company_size TEXT,
                    skills_json TEXT NOT NULL,
                    description_text TEXT NOT NULL,
                    field_confidence_json TEXT NOT NULL,
                    uncertainty_notes_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS long_texts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    source_label TEXT NOT NULL,
                    text TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_experience_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    card_json TEXT NOT NULL,
                    confirmed_summary TEXT,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    FOREIGN KEY(candidate_id) REFERENCES candidate_profiles(id)
                );

                CREATE TABLE IF NOT EXISTS resume_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id INTEGER NOT NULL,
                    job_id INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    draft_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(candidate_id) REFERENCES candidate_profiles(id),
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                """
            )

    def save_candidate_profile(self, profile: CandidateProfileInput) -> int:
        """保存候选人结构化档案，返回新建档案 ID。"""

        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO candidate_profiles (
                    name, status, education, experience_years, salary_floor_k,
                    expected_salary_k, skills_json, preferred_cities_json,
                    target_directions_json, unacceptable_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.name,
                    profile.status,
                    profile.education,
                    profile.experience_years,
                    profile.salary_floor_k,
                    profile.expected_salary_k,
                    json.dumps(profile.skills, ensure_ascii=False),
                    json.dumps(profile.preferred_cities, ensure_ascii=False),
                    json.dumps(profile.target_directions, ensure_ascii=False),
                    json.dumps(profile.unacceptable, ensure_ascii=False),
                ),
            )
            candidate_id = int(cursor.lastrowid)
            # 同一个事务里写 long_texts，避免 Windows 上 SQLite 多连接写入导致锁库。
            self._add_long_text(conn, "candidate_profile", candidate_id, "skills", " ".join(profile.skills))
            return candidate_id

    def get_candidate_profile(self, candidate_id: int) -> CandidateProfile:
        """按 ID 读取候选人档案，并把 JSON 字段还原成 Python 对象。"""

        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM candidate_profiles WHERE id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Candidate profile not found: {candidate_id}")
        return CandidateProfile(
            id=int(row["id"]),
            name=row["name"],
            status=row["status"],
            education=row["education"],
            experience_years=float(row["experience_years"]),
            salary_floor_k=row["salary_floor_k"],
            expected_salary_k=row["expected_salary_k"],
            skills=json.loads(row["skills_json"]),
            preferred_cities=json.loads(row["preferred_cities_json"]),
            target_directions=json.loads(row["target_directions_json"]),
            unacceptable=json.loads(row["unacceptable_json"]),
        )

    def update_candidate_profile(self, candidate_id: int, patch: CandidateProfilePatch) -> list[str]:
        """按 patch 局部更新候选人档案，返回实际更新的字段名。

        自动入库只合并消息中明确出现的字段：标量字段覆盖，列表字段去重追加，
        技能字段按技能名合并/更新熟练度。
        """

        current = self.get_candidate_profile(candidate_id)
        updated_fields: list[str] = []

        status = current.status
        education = current.education
        experience_years = current.experience_years
        salary_floor_k = current.salary_floor_k
        expected_salary_k = current.expected_salary_k
        skills = dict(current.skills)
        preferred_cities = list(current.preferred_cities)
        target_directions = list(current.target_directions)
        unacceptable = list(current.unacceptable)

        if patch.status:
            status = patch.status
            updated_fields.append("status")
        if patch.education:
            education = patch.education
            updated_fields.append("education")
        if patch.experience_years is not None:
            experience_years = patch.experience_years
            updated_fields.append("experience_years")
        if patch.salary_floor_k is not None:
            salary_floor_k = patch.salary_floor_k
            updated_fields.append("salary_floor_k")
        if patch.expected_salary_k is not None:
            expected_salary_k = patch.expected_salary_k
            updated_fields.append("expected_salary_k")
        if patch.skills:
            skills.update(patch.skills)
            updated_fields.append("skills")
        if patch.preferred_cities:
            preferred_cities = merge_unique(preferred_cities, patch.preferred_cities)
            updated_fields.append("preferred_cities")
        if patch.target_directions:
            target_directions = merge_unique(target_directions, patch.target_directions)
            updated_fields.append("target_directions")
        if patch.unacceptable:
            unacceptable = merge_unique(unacceptable, patch.unacceptable)
            updated_fields.append("unacceptable")

        if not updated_fields:
            return []

        with self.connect() as conn:
            conn.execute(
                """
                UPDATE candidate_profiles
                SET status = ?, education = ?, experience_years = ?,
                    salary_floor_k = ?, expected_salary_k = ?,
                    skills_json = ?, preferred_cities_json = ?,
                    target_directions_json = ?, unacceptable_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    education,
                    experience_years,
                    salary_floor_k,
                    expected_salary_k,
                    json.dumps(skills, ensure_ascii=False),
                    json.dumps(preferred_cities, ensure_ascii=False),
                    json.dumps(target_directions, ensure_ascii=False),
                    json.dumps(unacceptable, ensure_ascii=False),
                    candidate_id,
                ),
            )
            # 记录自动更新摘要，方便 RAG 和审计追溯“这次对话改了哪些结构化字段”。
            self._add_long_text(
                conn,
                "candidate_profile",
                candidate_id,
                "conversation_structured_update",
                "自动更新字段：" + "、".join(updated_fields),
            )
        return updated_fields

    def save_job_text(self, raw_text: str, source_url: str | None = None) -> ImportedJob:
        """保存一段职位原文。

        这里先调用规则解析器得到 `ImportedJob`，再同时保存原文、结构化字段和
        长文本副本。后续接入 LLM 时，可以替换 `parse_job_text` 的内部逻辑。
        """

        parsed = parse_job_text(raw_text, source_url=source_url)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO jobs (
                    raw_text, source_url, title, city, salary_min_k, salary_max_k,
                    salary_months, salary_unit, experience_min_years,
                    experience_max_years, experience_label, education,
                    company_name, industry, company_size, skills_json,
                    description_text, field_confidence_json, uncertainty_notes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parsed.raw_text,
                    parsed.source_url,
                    parsed.title,
                    parsed.city,
                    parsed.salary_min_k,
                    parsed.salary_max_k,
                    parsed.salary_months,
                    parsed.salary_unit,
                    parsed.experience_min_years,
                    parsed.experience_max_years,
                    parsed.experience_label,
                    parsed.education,
                    parsed.company_name,
                    parsed.industry,
                    parsed.company_size,
                    json.dumps(parsed.skills, ensure_ascii=False),
                    parsed.description_text,
                    json.dumps(parsed.field_confidence, ensure_ascii=False),
                    json.dumps(parsed.uncertainty_notes, ensure_ascii=False),
                ),
            )
            job_id = int(cursor.lastrowid)
            # 职位描述先进入 long_texts，后续可以同步到真正的向量数据库。
            self._add_long_text(conn, "job", job_id, "description", parsed.description_text)
        return self.get_job(job_id)

    def get_job(self, job_id: int) -> ImportedJob:
        """按 ID 读取标准化职位信息。"""

        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Job not found: {job_id}")
        return self._job_from_row(row)

    def list_jobs(self) -> list[ImportedJob]:
        """按导入顺序列出所有职位。

        批量匹配需要先拿到候选人主动导入过的职位池；这里仍然只读取本地数据，
        不会访问 BOSS 直聘网站。
        """

        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        return [self._job_from_row(row) for row in rows]

    def save_project_card(
        self,
        candidate_id: int,
        card: ProjectExperienceCard,
    ) -> ProjectExperienceRecord:
        """保存一张待确认项目经历卡片。

        自动分析得到的项目线索只进入 `project_experience_cards`，不会直接写入
        `candidate_profiles.skills_json` 等已确认事实字段。
        """

        now = now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO project_experience_cards (
                    candidate_id, status, project_name, card_json,
                    confirmed_summary, created_at, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    "待确认",
                    card.project_name,
                    json.dumps(asdict(card), ensure_ascii=False),
                    None,
                    now,
                    None,
                ),
            )
            record_id = int(cursor.lastrowid)
        return self.get_project_card(record_id)

    def get_project_card(self, record_id: int) -> ProjectExperienceRecord:
        """按 ID 读取一张项目经历卡片记录。"""

        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM project_experience_cards WHERE id = ?",
                (record_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Project experience card not found: {record_id}")
        return self._project_card_from_row(row)

    def list_project_cards(self, candidate_id: int) -> list[ProjectExperienceRecord]:
        """列出某个候选人的项目经历卡片。"""

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM project_experience_cards
                WHERE candidate_id = ?
                ORDER BY id
                """,
                (candidate_id,),
            ).fetchall()
        return [self._project_card_from_row(row) for row in rows]

    def confirm_project_card(
        self,
        record_id: int,
        confirmed_summary: str | None = None,
    ) -> ProjectExperienceRecord:
        """把项目经历卡片标记为已确认，并保存候选人的确认摘要。

        确认后的摘要会进入 `long_texts`，为后续向量检索/简历改写提供材料；
        但它仍然不会自动覆盖候选人档案中的学历、技能等结构化事实。
        """

        existing = self.get_project_card(record_id)
        summary = confirmed_summary if confirmed_summary is not None else existing.confirmed_summary
        confirmed_at = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE project_experience_cards
                SET status = ?, confirmed_summary = ?, confirmed_at = ?
                WHERE id = ?
                """,
                ("已确认", summary, confirmed_at, record_id),
            )
            # 如果候选人没有写确认摘要，就把卡片草稿作为可检索材料保存，
            # 但界面仍然应该提示它来自候选人确认过的卡片而不是原始档案事实。
            text_for_index = summary or project_card_index_text(existing.card)
            self._add_long_text(conn, "project_experience_card", record_id, "confirmed", text_for_index)
        return self.get_project_card(record_id)

    def save_resume_draft(
        self,
        candidate_id: int,
        job_id: int,
        draft: ResumeDraft,
    ) -> ResumeDraftRecord:
        """保存一个职位定制简历草稿版本。

        草稿版本单独保存，不会更新候选人档案，也不会覆盖历史版本。
        """

        created_at = now_iso()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS latest_version
                FROM resume_drafts
                WHERE candidate_id = ? AND job_id = ?
                """,
                (candidate_id, job_id),
            ).fetchone()
            version = int(row["latest_version"]) + 1
            cursor = conn.execute(
                """
                INSERT INTO resume_drafts (
                    candidate_id, job_id, version, status, draft_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    job_id,
                    version,
                    "需候选人确认",
                    json.dumps(asdict(draft), ensure_ascii=False),
                    created_at,
                ),
            )
            record_id = int(cursor.lastrowid)
            # 草稿全文进入 long_texts，后续可以用于“比较不同版本”或向量检索，
            # 但它的 entity_type 明确标记为草稿，不会被当成档案事实。
            self._add_long_text(conn, "resume_draft", record_id, f"v{version}", draft.content)
        return self.get_resume_draft(record_id)

    def get_resume_draft(self, record_id: int) -> ResumeDraftRecord:
        """按 ID 读取简历草稿版本。"""

        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM resume_drafts WHERE id = ?",
                (record_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Resume draft not found: {record_id}")
        return self._resume_draft_from_row(row)

    def list_resume_drafts(
        self,
        candidate_id: int,
        job_id: int | None = None,
    ) -> list[ResumeDraftRecord]:
        """列出候选人的简历草稿版本，可按职位过滤。"""

        with self.connect() as conn:
            if job_id is None:
                rows = conn.execute(
                    """
                    SELECT * FROM resume_drafts
                    WHERE candidate_id = ?
                    ORDER BY job_id, version
                    """,
                    (candidate_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM resume_drafts
                    WHERE candidate_id = ? AND job_id = ?
                    ORDER BY version
                    """,
                    (candidate_id, job_id),
                ).fetchall()
        return [self._resume_draft_from_row(row) for row in rows]

    def _job_from_row(self, row: sqlite3.Row) -> ImportedJob:
        """把 jobs 表的一行转换成 `ImportedJob`。"""

        return ImportedJob(
            id=int(row["id"]),
            raw_text=row["raw_text"],
            source_url=row["source_url"],
            title=row["title"],
            city=row["city"],
            salary_min_k=row["salary_min_k"],
            salary_max_k=row["salary_max_k"],
            salary_months=row["salary_months"],
            salary_unit=row["salary_unit"],
            experience_min_years=row["experience_min_years"],
            experience_max_years=row["experience_max_years"],
            experience_label=row["experience_label"],
            education=row["education"],
            company_name=row["company_name"],
            industry=row["industry"],
            company_size=row["company_size"],
            skills=json.loads(row["skills_json"]),
            description_text=row["description_text"],
            field_confidence=json.loads(row["field_confidence_json"]),
            uncertainty_notes=json.loads(row["uncertainty_notes_json"]),
        )

    def _project_card_from_row(self, row: sqlite3.Row) -> ProjectExperienceRecord:
        """把项目卡片表的一行转换成领域模型。"""

        card = ProjectExperienceCard(**json.loads(row["card_json"]))
        return ProjectExperienceRecord(
            id=int(row["id"]),
            candidate_id=int(row["candidate_id"]),
            status=row["status"],
            card=card,
            confirmed_summary=row["confirmed_summary"],
            created_at=row["created_at"],
            confirmed_at=row["confirmed_at"],
        )

    def _resume_draft_from_row(self, row: sqlite3.Row) -> ResumeDraftRecord:
        """把简历草稿表的一行转换成领域模型。"""

        draft = ResumeDraft(**json.loads(row["draft_json"]))
        return ResumeDraftRecord(
            id=int(row["id"]),
            candidate_id=int(row["candidate_id"]),
            job_id=int(row["job_id"]),
            version=int(row["version"]),
            status=row["status"],
            draft=draft,
            created_at=row["created_at"],
        )

    def add_long_text(self, entity_type: str, entity_id: int, source_label: str, text: str) -> int:
        """公开的长文本写入方法。

        对话式入库、项目描述、简历片段、HR 对话等长文本材料都通过这个入口登记。
        返回插入 ID，方便应用层告诉用户本次保存了哪些材料。
        """

        with self.connect() as conn:
            return self._add_long_text(conn, entity_type, entity_id, source_label, text)

    def list_long_texts(self, entity_types: list[str] | None = None) -> list[LongTextRecord]:
        """列出可同步到 RAG 索引的长文本材料。

        SQLite 仍然是长文本来源的登记处；RAG 层只从这里读取并建立语义索引。
        """

        with self.connect() as conn:
            if entity_types:
                placeholders = ", ".join("?" for _ in entity_types)
                rows = conn.execute(
                    f"""
                    SELECT * FROM long_texts
                    WHERE entity_type IN ({placeholders})
                    ORDER BY id
                    """,
                    tuple(entity_types),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM long_texts ORDER BY id").fetchall()
        return [long_text_from_row(row) for row in rows]

    def get_long_texts_by_ids(self, ids: list[int]) -> list[LongTextRecord]:
        """按 ID 读取长文本材料，供增量 RAG 索引使用。

        对话式入库已经知道本次新增了哪些 `long_text_id`，因此增量索引不需要再
        读取整张 `long_texts` 表。
        """

        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM long_texts
                WHERE id IN ({placeholders})
                ORDER BY id
                """,
                tuple(ids),
            ).fetchall()
        return [long_text_from_row(row) for row in rows]

    def _add_long_text(
        self,
        conn: sqlite3.Connection,
        entity_type: str,
        entity_id: int,
        source_label: str,
        text: str,
    ) -> int:
        """在已有连接中写入长文本，供事务内复用。"""

        cursor = conn.execute(
            """
            INSERT INTO long_texts (entity_type, entity_id, source_label, text)
            VALUES (?, ?, ?, ?)
            """,
            (entity_type, entity_id, source_label, text),
        )
        return int(cursor.lastrowid)


def now_iso() -> str:
    """返回秒级 ISO 时间字符串，用于记录本地确认时间。"""

    return datetime.now().isoformat(timespec="seconds")


def long_text_from_row(row: sqlite3.Row) -> LongTextRecord:
    """把 SQLite 行转换为长文本记录。"""

    return LongTextRecord(
        id=int(row["id"]),
        entity_type=row["entity_type"],
        entity_id=int(row["entity_id"]),
        source_label=row["source_label"],
        text=row["text"],
    )


def project_card_index_text(card: ProjectExperienceCard) -> str:
    """把已确认项目卡片整理成一段可进入长文本检索的材料。"""

    parts = [
        card.project_name,
        "技术栈：" + "、".join(card.detected_tech_stack),
        "核心功能：" + "、".join(card.detected_core_features),
        "职责草稿：" + "；".join(card.responsibility_draft),
        "亮点草稿：" + "；".join(card.highlight_draft),
        "简历表达草稿：" + "；".join(card.resume_expression_draft),
    ]
    # 过滤空段落，避免向长文本表写入一堆无意义的空字段。
    return "\n".join(part for part in parts if part.strip())


def merge_unique(existing: list[str], incoming: list[str]) -> list[str]:
    """按原顺序合并列表并去重。"""

    merged = list(existing)
    for item in incoming:
        if item and item not in merged:
            merged.append(item)
    return merged
