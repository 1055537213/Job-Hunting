"""PostgreSQL + pgvector RAG 后端的集成行为测试。

这些测试只在显式提供 ``JOB_AGENT_PGVECTOR_TEST_DATABASE_URL`` 时连接真实
PostgreSQL，默认的单元测试环境不会因本机未启动 Docker 而失败。
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event, get_ident

import pytest
import sqlalchemy as sa

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.database_schema import accounts, rag_chunks
from job_hunting_agent.evals.rag_eval import (
    EvidenceRef,
    RAGEvalCase,
    evaluate_rag_cases,
)
from job_hunting_agent.models import CandidateProfileInput
from job_hunting_agent.pgvector_rag import (
    PgVectorKnowledgeBase,
    _extract_exact_retrieval_tokens,
    _merge_text_retrieval_rows,
)
from job_hunting_agent.rag import LocalHashEmbeddings


def test_exact_retrieval_tokens_keep_numeric_and_identifier_signals() -> None:
    """数字、公差、编号和全大写标签进入精确补召回，普通词不会进入。"""

    assert _extract_exact_retrieval_tokens(
        "ZX-2048 直径 18.00 mm，上偏差 +0.02，下偏差 -0.01，CURRENT CAPTURE"
    ) == ("ZX-2048", "18.00", "0.02", "0.01", "CURRENT", "CAPTURE")


def test_exact_rows_share_the_text_top_k_budget_with_vector_rows() -> None:
    """精确通道补入向量候选时，文字候选总数仍不超过 Top-K。"""

    def row(long_text_id: int, chunk_index: int, content: str) -> dict[str, object]:
        return {
            "long_text_id": long_text_id,
            "chunk_index": chunk_index,
            "content": content,
        }

    vector_rows = [row(index, 0, f"vector-{index}") for index in range(1, 5)]
    exact_rows = [row(99, 0, "exact 18.00 mm"), vector_rows[0]]

    merged = _merge_text_retrieval_rows(vector_rows, exact_rows, limit=4)

    assert [item["long_text_id"] for item in merged] == [99, 1, 2, 3]


def test_pgvector_rebuilds_and_searches_only_the_requested_account(database_url):
    """pgvector 重建后只能召回当前账号已经登记的长文本证据。"""

    app = JobHuntingApp(database_url=database_url)
    app.initialize()

    account_ids: list[int] = []
    try:
        first_account = app.store.create_account(
            f"pgvector-first-{uuid.uuid4().hex}@example.com",
            "hashed-password",
        )
        second_account = app.store.create_account(
            f"pgvector-second-{uuid.uuid4().hex}@example.com",
            "hashed-password",
        )
        account_ids.extend([first_account.id, second_account.id])
        first_candidate_id = app.save_candidate_profile(
            build_candidate_input("第一位候选人"),
            account_id=first_account.id,
        )
        second_candidate_id = app.save_candidate_profile(
            build_candidate_input("第二位候选人"),
            account_id=second_account.id,
        )
        first_long_text_id = app.store.add_long_text(
            "conversation_message",
            first_candidate_id,
            "first-account-note",
            "第一位候选人负责 FastAPI 职位解析和匹配排序。",
            account_id=first_account.id,
            candidate_id=first_candidate_id,
        )
        second_long_text_id = app.store.add_long_text(
            "conversation_message",
            second_candidate_id,
            "second-account-note",
            "第二位候选人负责 FastAPI 职位解析和匹配排序。",
            account_id=second_account.id,
            candidate_id=second_candidate_id,
        )
        knowledge_base = PgVectorKnowledgeBase(
            app.store.engine,
            embeddings=LocalHashEmbeddings(dimensions=16),
        )

        stats = knowledge_base.rebuild(
            app.store.get_long_texts_by_ids([first_long_text_id], account_id=first_account.id),
            account_id=first_account.id,
        )
        knowledge_base.index_long_texts(
            app.store.get_long_texts_by_ids([second_long_text_id], account_id=second_account.id),
            account_id=second_account.id,
        )
        first_results = knowledge_base.search(
            "FastAPI 职位解析",
            account_id=first_account.id,
        )

        assert stats.mode == "rebuild"
        assert stats.collection_name == "rag_chunks"
        assert [result.long_text_id for result in first_results] == [first_long_text_id]
        assert all("第一位候选人" in result.content for result in first_results)
    finally:
        # 测试账号是整条数据链的根节点，删除后由外键级联清理候选人、长文本和 RAG chunk。
        with app.store.engine.begin() as connection:
            connection.execute(sa.delete(accounts).where(accounts.c.id.in_(account_ids)))
        app.store.close()


def test_pgvector_rejects_long_text_from_a_different_account(database_url):
    """传入账号不能改写已经归属另一账号的长文本证据。"""

    app = JobHuntingApp(database_url=database_url)
    app.initialize()
    account_ids: list[int] = []
    try:
        first_account = app.store.create_account(
            f"pgvector-owner-first-{uuid.uuid4().hex}@example.com",
            "hashed-password",
        )
        second_account = app.store.create_account(
            f"pgvector-owner-second-{uuid.uuid4().hex}@example.com",
            "hashed-password",
        )
        account_ids.extend([first_account.id, second_account.id])
        second_candidate_id = app.save_candidate_profile(
            build_candidate_input("材料实际归属的候选人"),
            account_id=second_account.id,
        )
        second_long_text_id = app.store.add_long_text(
            "conversation_message",
            second_candidate_id,
            "ownership-note",
            "这段证据属于第二个账号，不能被第一个账号索引。",
            account_id=second_account.id,
            candidate_id=second_candidate_id,
        )
        source = app.store.get_long_texts_by_ids(
            [second_long_text_id],
            account_id=second_account.id,
        )
        knowledge_base = PgVectorKnowledgeBase(
            app.store.engine,
            embeddings=LocalHashEmbeddings(dimensions=16),
        )

        with pytest.raises(ValueError, match="账号"):
            knowledge_base.index_long_texts(source, account_id=first_account.id)
    finally:
        with app.store.engine.begin() as connection:
            connection.execute(sa.delete(accounts).where(accounts.c.id.in_(account_ids)))
        app.store.close()


def test_app_uses_pgvector_backend_when_its_store_is_postgresql(database_url, tmp_path):
    """应用门面通过 PostgreSQL + pgvector 完成索引和检索。"""

    app = JobHuntingApp(database_url=database_url)
    app.initialize()
    app.model_gateway.embeddings = lambda _context: LocalHashEmbeddings(dimensions=16)
    app.model_gateway.reranker = lambda _context: None
    account_ids: list[int] = []
    try:
        account = app.store.create_account(
            f"pgvector-app-{uuid.uuid4().hex}@example.com",
            "hashed-password",
        )
        account_ids.append(account.id)
        candidate_id = app.save_candidate_profile(
            build_candidate_input("应用层候选人"),
            account_id=account.id,
        )
        long_text_id = app.store.add_long_text(
            "conversation_message",
            candidate_id,
            "app-backend-note",
            "应用层通过 PostgreSQL pgvector 保存职位解析证据。",
            account_id=account.id,
            candidate_id=candidate_id,
        )

        stats = app.rebuild_rag_index(account_id=account.id)
        results = app.search_rag("PostgreSQL 职位解析", account_id=account.id)

        assert stats.persist_directory == "postgresql+pgvector"
        assert long_text_id in [result.long_text_id for result in results]
    finally:
        with app.store.engine.begin() as connection:
            connection.execute(sa.delete(accounts).where(accounts.c.id.in_(account_ids)))
        app.store.close()


def test_pgvector_incremental_indexing_is_idempotent_and_supports_deletion(database_url):
    """重复索引同一长文本不会重复召回，删除时会移除它的派生 chunk。"""

    app = JobHuntingApp(database_url=database_url)
    app.initialize()
    account_ids: list[int] = []
    try:
        account = app.store.create_account(
            f"pgvector-incremental-{uuid.uuid4().hex}@example.com",
            "hashed-password",
        )
        account_ids.append(account.id)
        candidate_id = app.save_candidate_profile(
            build_candidate_input("增量索引候选人"),
            account_id=account.id,
        )
        long_text_id = app.store.add_long_text(
            "conversation_message",
            candidate_id,
            "incremental-note",
            "候选人维护了一个唯一的 pgvector 增量索引验证笔记。",
            account_id=account.id,
            candidate_id=candidate_id,
        )
        source = app.store.get_long_texts_by_ids([long_text_id], account_id=account.id)
        knowledge_base = PgVectorKnowledgeBase(
            app.store.engine,
            embeddings=LocalHashEmbeddings(dimensions=16),
        )

        first_stats = knowledge_base.index_long_texts(source, account_id=account.id)
        second_stats = knowledge_base.index_long_texts(source, account_id=account.id)
        before_delete = knowledge_base.search(
            "唯一 pgvector 增量索引验证笔记",
            top_n=10,
            entity_types=["conversation_message"],
            account_id=account.id,
        )
        deleted_count = knowledge_base.delete_long_texts([long_text_id], account_id=account.id)
        after_delete = knowledge_base.search(
            "唯一 pgvector 增量索引验证笔记",
            top_n=10,
            entity_types=["conversation_message"],
            account_id=account.id,
        )

        assert first_stats.mode == "incremental"
        assert second_stats.mode == "incremental"
        assert [result.long_text_id for result in before_delete].count(long_text_id) == 1
        assert deleted_count == 1
        assert long_text_id not in [result.long_text_id for result in after_delete]
    finally:
        with app.store.engine.begin() as connection:
            connection.execute(sa.delete(accounts).where(accounts.c.id.in_(account_ids)))
        app.store.close()


def test_pgvector_incremental_reindex_removes_stale_tail_chunks(database_url):
    """正文缩短后，旧切分尾块不能继续作为可检索证据。"""

    app = JobHuntingApp(database_url=database_url)
    app.initialize()
    account_ids: list[int] = []
    try:
        account = app.store.create_account(
            f"pgvector-stale-tail-{uuid.uuid4().hex}@example.com",
            "hashed-password",
        )
        account_ids.append(account.id)
        candidate_id = app.save_candidate_profile(
            build_candidate_input("旧尾块验证候选人"),
            account_id=account.id,
        )
        long_text_id = app.store.add_long_text(
            "conversation_message",
            candidate_id,
            "stale-tail-note",
            "保留的项目事实。" + "中间项目说明。" * 320 + "待删除的旧尾块唯一标记 stale-tail-unique-90210。",
            account_id=account.id,
            candidate_id=candidate_id,
        )
        source = app.store.get_long_texts_by_ids([long_text_id], account_id=account.id)[0]
        knowledge_base = PgVectorKnowledgeBase(
            app.store.engine,
            embeddings=LocalHashEmbeddings(dimensions=16),
        )

        first_stats = knowledge_base.index_long_texts([source], account_id=account.id)
        old_tail_results = knowledge_base.search(
            "stale-tail-unique-90210",
            top_n=10,
            account_id=account.id,
        )
        shortened = replace(source, text="保留的项目事实。")
        second_stats = knowledge_base.index_long_texts([shortened], account_id=account.id)
        after_reindex = knowledge_base.search(
            "stale-tail-unique-90210",
            top_n=10,
            account_id=account.id,
        )

        assert first_stats.chunk_count > second_stats.chunk_count
        assert any("stale-tail-unique-90210" in result.content for result in old_tail_results)
        assert all("stale-tail-unique-90210" not in result.content for result in after_reindex)
    finally:
        with app.store.engine.begin() as connection:
            connection.execute(sa.delete(accounts).where(accounts.c.id.in_(account_ids)))
        app.store.close()


def test_pgvector_concurrent_reindex_serializes_same_source(database_url, monkeypatch):
    """同一来源的并发替换必须串行化，后提交版本不能留下另一版本的尾块。"""

    app = JobHuntingApp(database_url=database_url)
    app.initialize()
    account_ids: list[int] = []
    release_first_upsert = Event()
    first_upsert_entered = Event()
    second_lock_attempted = Event()
    listener_registered = False
    try:
        account = app.store.create_account(
            f"pgvector-concurrent-reindex-{uuid.uuid4().hex}@example.com",
            "hashed-password",
        )
        account_ids.append(account.id)
        candidate_id = app.save_candidate_profile(
            build_candidate_input("并发索引验证候选人"),
            account_id=account.id,
        )
        long_text_id = app.store.add_long_text(
            "conversation_message",
            candidate_id,
            "concurrent-reindex-note",
            "初始内容。",
            account_id=account.id,
            candidate_id=candidate_id,
        )
        source = app.store.get_long_texts_by_ids([long_text_id], account_id=account.id)[0]
        long_version = replace(
            source,
            text="并发长版本。" + "中间项目描述。" * 320 + "不应残留的并发尾块 concurrency-tail-731。",
        )
        short_version = replace(source, text="并发短版本是最终版本。")
        first_backend = PgVectorKnowledgeBase(
            app.store.engine,
            embeddings=LocalHashEmbeddings(dimensions=16),
        )
        second_backend = PgVectorKnowledgeBase(
            app.store.engine,
            embeddings=LocalHashEmbeddings(dimensions=16),
        )
        original_upsert = first_backend._upsert_rows

        def blocking_first_upsert(connection, rows):
            first_upsert_entered.set()
            if not release_first_upsert.wait(timeout=10):
                raise TimeoutError("等待并发索引测试释放首个事务超时。")
            return original_upsert(connection, rows)

        monkeypatch.setattr(first_backend, "_upsert_rows", blocking_first_upsert)
        second_thread_ident: list[int] = []

        def observe_second_lock(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            if (
                second_thread_ident
                and get_ident() == second_thread_ident[0]
                and "pg_advisory_xact_lock" in statement
            ):
                second_lock_attempted.set()

        sa.event.listen(app.store.engine, "before_cursor_execute", observe_second_lock)
        listener_registered = True

        def index_short_version():
            second_thread_ident.append(get_ident())
            return second_backend.index_long_texts([short_version], account_id=account.id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            try:
                first_future = executor.submit(
                    first_backend.index_long_texts,
                    [long_version],
                    account.id,
                )
                assert first_upsert_entered.wait(timeout=5)
                second_future = executor.submit(index_short_version)
                assert second_lock_attempted.wait(timeout=5)
                assert not second_future.done()
            finally:
                release_first_upsert.set()
            first_stats = first_future.result(timeout=10)
            second_stats = second_future.result(timeout=10)

        with app.store.engine.connect() as connection:
            indexed_contents = connection.execute(
                sa.select(rag_chunks.c.content)
                .where(rag_chunks.c.long_text_id == long_text_id)
                .order_by(rag_chunks.c.chunk_index)
            ).scalars().all()

        assert first_stats.chunk_count > second_stats.chunk_count
        assert len(indexed_contents) == second_stats.chunk_count
        assert all("concurrency-tail-731" not in content for content in indexed_contents)
        assert "并发短版本是最终版本" in "\n".join(indexed_contents)
    finally:
        release_first_upsert.set()
        if listener_registered:
            sa.event.remove(app.store.engine, "before_cursor_execute", observe_second_lock)
        with app.store.engine.begin() as connection:
            connection.execute(sa.delete(accounts).where(accounts.c.id.in_(account_ids)))
        app.store.close()


def test_semantic_chunking_passes_fixed_pgvector_retrieval_cases(database_url):
    """改切分后，简历、表格和 OCR/PDF 黄金证据仍能通过真实 pgvector 路径召回。"""

    app = JobHuntingApp(database_url=database_url)
    app.initialize()
    account_ids: list[int] = []
    try:
        account = app.store.create_account(
            f"pgvector-semantic-golden-{uuid.uuid4().hex}@example.com",
            "hashed-password",
        )
        account_ids.append(account.id)
        candidate_id = app.save_candidate_profile(
            build_candidate_input("语义切分黄金用例候选人"),
            account_id=account.id,
        )
        sources = [
            (
                "resume_artifact",
                "golden:resume-project",
                "项目经历\n求职助手 Agent\n负责 semanticgoldenfastapi 接口与职位解析。",
            ),
            (
                "project_archive_file",
                "golden:spreadsheet-parameter",
                "[工作表 Parameters]\nName\tValue\tUnit\nsemanticgoldentolerance\t0.02\tmm",
            ),
            (
                "project_archive_file",
                "golden:ocr-drawing",
                "[第 2 页]\n[OCR 文字]\nsemanticgoldenpumpaxis 泵轴外径尺寸标注为 18 mm。",
            ),
        ]
        long_text_ids = [
            app.store.add_long_text(
                entity_type,
                candidate_id,
                source_label,
                text,
                account_id=account.id,
                candidate_id=candidate_id,
            )
            for entity_type, source_label, text in sources
        ]
        knowledge_base = PgVectorKnowledgeBase(
            app.store.engine,
            embeddings=LocalHashEmbeddings(dimensions=128),
        )
        knowledge_base.index_long_texts(
            app.store.get_long_texts_by_ids(long_text_ids, account_id=account.id),
            account_id=account.id,
        )
        cases = [
            RAGEvalCase(
                id="resume-project",
                query="semanticgoldenfastapi",
                expected=(EvidenceRef(source_label="golden:resume-project"),),
                top_n=1,
            ),
            RAGEvalCase(
                id="spreadsheet-parameter",
                query="semanticgoldentolerance",
                expected=(EvidenceRef(source_label="golden:spreadsheet-parameter"),),
                top_n=1,
            ),
            RAGEvalCase(
                id="ocr-drawing",
                query="semanticgoldenpumpaxis",
                expected=(EvidenceRef(source_label="golden:ocr-drawing"),),
                top_n=1,
            ),
        ]

        report = evaluate_rag_cases(
            cases,
            lambda case, top_n: knowledge_base.search(
                case.query,
                top_n=top_n,
                entity_types=list(case.entity_types) or None,
                account_id=account.id,
            ),
        )

        assert report.all_passed
        assert report.mean_recall_at_n == 1.0
        assert report.mean_reciprocal_rank == 1.0
        ocr_results = knowledge_base.search(
            "semanticgoldenpumpaxis",
            top_n=1,
            account_id=account.id,
        )
        assert ocr_results[0].page_number == 2
    finally:
        with app.store.engine.begin() as connection:
            connection.execute(sa.delete(accounts).where(accounts.c.id.in_(account_ids)))
        app.store.close()


def test_pgvector_does_not_compare_vectors_from_a_different_embedding_identity(database_url):
    """切换 Embedding 后旧向量被隔离，检索不会因维度不同而报错或混用结果。"""

    app = JobHuntingApp(database_url=database_url)
    app.initialize()
    account_ids: list[int] = []
    try:
        account = app.store.create_account(
            f"pgvector-model-switch-{uuid.uuid4().hex}@example.com",
            "hashed-password",
        )
        account_ids.append(account.id)
        candidate_id = app.save_candidate_profile(
            build_candidate_input("模型切换候选人"),
            account_id=account.id,
        )
        long_text_id = app.store.add_long_text(
            "conversation_message",
            candidate_id,
            "model-switch-note",
            "这条材料只由十六维 embedding 建立索引。",
            account_id=account.id,
            candidate_id=candidate_id,
        )
        source = app.store.get_long_texts_by_ids([long_text_id], account_id=account.id)
        original_backend = PgVectorKnowledgeBase(
            app.store.engine,
            embeddings=LocalHashEmbeddings(dimensions=16),
        )
        replacement_backend = PgVectorKnowledgeBase(
            app.store.engine,
            embeddings=AlternateLocalHashEmbeddings(dimensions=16),
        )

        original_backend.index_long_texts(source, account_id=account.id)
        results = replacement_backend.search(
            "十六维 embedding 建立索引",
            account_id=account.id,
        )

        assert results == []
    finally:
        with app.store.engine.begin() as connection:
            connection.execute(sa.delete(accounts).where(accounts.c.id.in_(account_ids)))
        app.store.close()


def test_app_deletion_relies_on_postgresql_cascade_for_pgvector_chunks(database_url):
    """删除候选人档案时，PostgreSQL 外键应同步删除其 RAG 证据。"""

    app = JobHuntingApp(database_url=database_url)
    app.initialize()
    app.model_gateway.embeddings = lambda _context: LocalHashEmbeddings(dimensions=16)
    account_ids: list[int] = []
    try:
        account = app.store.create_account(
            f"pgvector-cascade-{uuid.uuid4().hex}@example.com",
            "hashed-password",
        )
        account_ids.append(account.id)
        candidate_id = app.save_candidate_profile(
            build_candidate_input("级联删除候选人"),
            account_id=account.id,
        )
        long_text_id = app.store.add_long_text(
            "conversation_message",
            candidate_id,
            "cascade-note",
            "这条候选人资料需要验证 pgvector 级联删除。",
            account_id=account.id,
            candidate_id=candidate_id,
        )
        app.index_rag_long_texts([long_text_id], account_id=account.id)

        deletion = app.delete_candidate_profile(
            candidate_id,
            account_id=account.id,
        )
        knowledge_base = PgVectorKnowledgeBase(
            app.store.engine,
            embeddings=LocalHashEmbeddings(dimensions=16),
        )
        remaining = knowledge_base.search(
            "pgvector 级联删除",
            entity_types=["conversation_message"],
            account_id=account.id,
        )

        assert deletion["rag_cleanup"] == "database_cascade"
        assert remaining == []
    finally:
        with app.store.engine.begin() as connection:
            connection.execute(sa.delete(accounts).where(accounts.c.id.in_(account_ids)))
        app.store.close()


def build_candidate_input(name: str) -> CandidateProfileInput:
    """构造满足候选人档案最小字段要求的测试数据。"""

    return CandidateProfileInput(
        name=name,
        status="在职",
        education="本科",
        experience_years=2.0,
        skills={"Python": "项目使用"},
        preferred_cities=["杭州"],
        salary_floor_k=12,
        expected_salary_k=16,
        target_directions=["Python 后端开发"],
    )


class AlternateLocalHashEmbeddings(LocalHashEmbeddings):
    """维度相同但模型身份不同的测试 Embedding，用于验证模型隔离而非维度隔离。"""

    def __init__(self, dimensions: int):
        super().__init__(dimensions=dimensions)
        self.model = "alternate-local-hash-model"
        self.embeddings_url = "https://alternate-embedding.example/v1/embeddings"
