from __future__ import annotations

from langchain_core.documents import Document

from job_hunting_agent.rag import split_rag_documents


def test_semantic_chunking_keeps_markdown_heading_with_its_body() -> None:
    documents = [
        Document(
            page_content=(
                "## 项目职责\n"
                "负责设计职位解析接口，使用 FastAPI 和 PostgreSQL 完成数据处理。\n\n"
                "通过缓存和异步任务降低接口响应延迟。"
            ),
            metadata={
                "long_text_id": 7,
                "entity_type": "project_experience_card",
                "entity_id": 11,
                "source_label": "resume.pdf#page=2",
                "account_id": 3,
            },
        )
    ]

    chunks = split_rag_documents(documents)

    assert len(chunks) == 1
    assert chunks[0].page_content.startswith("## 项目职责")
    assert "FastAPI" in chunks[0].page_content
    assert "降低接口响应延迟" in chunks[0].page_content
    assert chunks[0].metadata["chunking_version"] == "semantic-v1"
    assert chunks[0].metadata["section_title"] == "项目职责"
    assert chunks[0].metadata["semantic_type"] == "paragraph"
    assert chunks[0].metadata["source_page"] == 2


def test_semantic_chunking_keeps_lists_code_and_tables_as_complete_units() -> None:
    document = Document(
        page_content=(
            "## 实现细节\n"
            "- 设计职位解析接口\n"
            "- 增加异步任务和缓存\n\n"
            "```python\n"
            "def parse_job(text):\n"
            "    return normalize(text)\n"
            "```\n\n"
            "| 技术 | 用途 |\n"
            "| --- | --- |\n"
            "| FastAPI | API |\n"
            "| PostgreSQL | 存储 |"
        ),
        metadata={"long_text_id": 8, "entity_type": "project", "entity_id": 12, "source_label": "project.md"},
    )

    chunks = split_rag_documents([document])

    assert [chunk.metadata["semantic_type"] for chunk in chunks] == ["bullet_list", "code_block", "table"]
    assert chunks[0].page_content.count("- ") == 2
    assert "def parse_job(text):" in chunks[1].page_content
    assert "| FastAPI | API |" in chunks[2].page_content
    assert all(chunk.metadata["chunking_version"] == "semantic-v1" for chunk in chunks)


def test_semantic_chunking_uses_sentence_boundaries_before_hard_length_fallback() -> None:
    document = Document(
        page_content="## 项目成果\n" + "第一句描述项目背景。第二句描述实现方案！第三句描述最终结果？" * 80,
        metadata={"long_text_id": 9, "entity_type": "resume_artifact", "entity_id": 13, "source_label": "resume.pdf#page=3"},
    )

    chunks = split_rag_documents([document])

    assert len(chunks) > 1
    assert all(len(chunk.page_content) <= 1_400 for chunk in chunks)
    assert all(chunk.metadata["source_page"] == 3 for chunk in chunks)
    assert all(chunk.metadata["fragment_count"] == len(chunks) for chunk in chunks)
    assert [chunk.metadata["chunk_index"] for chunk in chunks] == list(range(len(chunks)))


def test_semantic_chunking_enforces_hard_cap_for_an_extremely_long_heading() -> None:
    heading = "H" * 1_500
    document = Document(
        page_content=f"## {heading}\n正文仍需保留 unique-heading-body。",
        metadata={"long_text_id": 90, "entity_type": "project", "entity_id": 90, "source_label": "long-heading.md"},
    )

    chunks = split_rag_documents([document])

    assert len(chunks) > 1
    assert all(len(chunk.page_content) <= 1_400 for chunk in chunks)
    assert "unique-heading-body" in "\n".join(chunk.page_content for chunk in chunks)


def test_semantic_chunking_enforces_hard_cap_for_an_extremely_long_code_fence() -> None:
    language_tag = "x" * 1_450
    document = Document(
        page_content=f"```{language_tag}\nprint('unique-code-body')\n```",
        metadata={"long_text_id": 91, "entity_type": "project", "entity_id": 91, "source_label": "long-fence.md"},
    )

    chunks = split_rag_documents([document])

    assert len(chunks) > 1
    assert all(len(chunk.page_content) <= 1_400 for chunk in chunks)
    assert "unique-code-body" in "\n".join(chunk.page_content for chunk in chunks)


def test_page_markers_update_source_page_without_cross_page_chunks() -> None:
    document = Document(
        page_content=(
            "[page=1]\n项目第一页的技术栈和职责。\n\n"
            "[page=2]\n项目第二页的项目结果和指标。"
        ),
        metadata={"long_text_id": 10, "entity_type": "resume_artifact", "entity_id": 14, "source_label": "resume.pdf"},
    )

    chunks = split_rag_documents([document])

    assert [chunk.metadata["source_page"] for chunk in chunks] == [1, 2]
    assert "项目第一页" in chunks[0].page_content
    assert "项目第二页" in chunks[1].page_content


def test_project_pdf_page_markers_are_supported() -> None:
    document = Document(
        page_content="[第 4 页]\n项目证据中的图纸说明和尺寸标注。",
        metadata={"long_text_id": 11, "entity_type": "project_archive_file", "entity_id": 15, "source_label": "drawing.pdf"},
    )

    chunks = split_rag_documents([document])

    assert len(chunks) == 1
    assert chunks[0].metadata["source_page"] == 4
    assert "[第 4 页]" not in chunks[0].page_content
    assert "图纸说明" in chunks[0].page_content


def test_semantic_chunking_preserves_source_metadata_for_each_fragment() -> None:
    document = Document(
        page_content="## 技术栈\n" + ("FastAPI、PostgreSQL、Redis、Celery、Docker，" * 200),
        metadata={
            "long_text_id": 12,
            "entity_type": "project_experience_card",
            "entity_id": 16,
            "source_label": "project.md#page=5",
            "account_id": 3,
            "candidate_id": 4,
        },
    )

    chunks = split_rag_documents([document])

    assert len(chunks) > 1
    assert all(chunk.metadata["long_text_id"] == 12 for chunk in chunks)
    assert all(chunk.metadata["candidate_id"] == 4 for chunk in chunks)
    assert all(chunk.metadata["section_title"] == "技术栈" for chunk in chunks)
    assert [chunk.metadata["fragment_index"] for chunk in chunks] == list(range(len(chunks)))


def test_spreadsheet_sections_keep_sheet_name_and_tabular_rows() -> None:
    document = Document(
        page_content=(
            "[工作表 Parameters]\n"
            "Name\tValue\tUnit\n"
            "Tolerance\t0.02\tmm\n"
            "Pressure\t10\tMPa\n\n"
            "[工作表 Summary]\n"
            "Metric\tResult\n"
            "Latency\t120ms"
        ),
        metadata={"long_text_id": 13, "entity_type": "project_archive_file", "entity_id": 17, "source_label": "parameters.xlsx"},
    )

    chunks = split_rag_documents([document])

    assert [chunk.metadata["section_title"] for chunk in chunks] == ["工作表 Parameters", "工作表 Summary"]
    assert [chunk.metadata["semantic_type"] for chunk in chunks] == ["table", "table"]
    assert "Tolerance\t0.02\tmm" in chunks[0].page_content
    assert "Latency\t120ms" in chunks[1].page_content


def test_plain_resume_section_labels_become_semantic_boundaries() -> None:
    document = Document(
        page_content=(
            "项目经历\n"
            "求职助手 Agent\n"
            "负责 FastAPI 接口和 PostgreSQL 数据建模。\n\n"
            "项目成果\n"
            "将核心流程延迟降低到 120ms。"
        ),
        metadata={"long_text_id": 14, "entity_type": "resume_artifact", "entity_id": 18, "source_label": "resume.docx"},
    )

    chunks = split_rag_documents([document])

    assert [chunk.metadata["section_title"] for chunk in chunks] == ["项目经历", "项目成果"]
    assert "FastAPI" in chunks[0].page_content
    assert "120ms" in chunks[1].page_content
