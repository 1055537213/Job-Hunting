"""PostgreSQL + pgvector RAG 后端的集成行为测试。

这些测试只在显式提供 ``JOB_AGENT_PGVECTOR_TEST_DATABASE_URL`` 时连接真实
PostgreSQL，默认的单元测试环境不会因本机未启动 Docker 而失败。
"""

from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa

from job_hunting_agent.app import JobHuntingApp
from job_hunting_agent.database_migrations import upgrade_database
from job_hunting_agent.database_schema import accounts
from job_hunting_agent.models import CandidateProfileInput
from job_hunting_agent.pgvector_rag import PgVectorKnowledgeBase
from job_hunting_agent.rag import LocalHashEmbeddings


POSTGRES_TEST_URL_ENV = "JOB_AGENT_PGVECTOR_TEST_DATABASE_URL"
POSTGRES_TEST_URL = os.environ.get(POSTGRES_TEST_URL_ENV)

pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason=f"需要显式设置 {POSTGRES_TEST_URL_ENV} 才运行 pgvector 集成测试。",
)


def test_pgvector_rebuilds_and_searches_only_the_requested_account(tmp_path):
    """pgvector 重建后只能召回当前账号已经登记的长文本证据。"""

    assert POSTGRES_TEST_URL is not None
    upgrade_database(POSTGRES_TEST_URL)
    app = JobHuntingApp(tmp_path / "ignored.db", database_url=POSTGRES_TEST_URL)
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


def test_pgvector_rejects_long_text_from_a_different_account(tmp_path):
    """传入账号不能改写已经归属另一账号的长文本证据。"""

    assert POSTGRES_TEST_URL is not None
    upgrade_database(POSTGRES_TEST_URL)
    app = JobHuntingApp(tmp_path / "ignored.db", database_url=POSTGRES_TEST_URL)
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


def test_app_uses_pgvector_backend_when_its_store_is_postgresql(tmp_path):
    """应用门面在 PostgreSQL 模式下不应创建 Chroma 目录。"""

    assert POSTGRES_TEST_URL is not None
    upgrade_database(POSTGRES_TEST_URL)
    app = JobHuntingApp(tmp_path / "ignored.db", database_url=POSTGRES_TEST_URL)
    app.initialize()
    app.model_gateway.embeddings = lambda _context: LocalHashEmbeddings(dimensions=16)
    app.model_gateway.reranker = lambda _context: None
    account_ids: list[int] = []
    ignored_chroma_dir = tmp_path / "should-not-be-created"
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

        stats = app.rebuild_rag_index(ignored_chroma_dir, account_id=account.id)
        results = app.search_rag(
            "PostgreSQL 职位解析",
            ignored_chroma_dir,
            account_id=account.id,
        )

        assert stats.persist_directory == "postgresql+pgvector"
        assert long_text_id in [result.long_text_id for result in results]
        assert not ignored_chroma_dir.exists()
    finally:
        with app.store.engine.begin() as connection:
            connection.execute(sa.delete(accounts).where(accounts.c.id.in_(account_ids)))
        app.store.close()


def test_pgvector_incremental_indexing_is_idempotent_and_supports_deletion(tmp_path):
    """重复索引同一长文本不会重复召回，删除时会移除它的派生 chunk。"""

    assert POSTGRES_TEST_URL is not None
    upgrade_database(POSTGRES_TEST_URL)
    app = JobHuntingApp(tmp_path / "ignored.db", database_url=POSTGRES_TEST_URL)
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
            top_k=10,
            entity_types=["conversation_message"],
            account_id=account.id,
        )
        deleted_count = knowledge_base.delete_long_texts([long_text_id], account_id=account.id)
        after_delete = knowledge_base.search(
            "唯一 pgvector 增量索引验证笔记",
            top_k=10,
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


def test_pgvector_does_not_compare_vectors_from_a_different_embedding_identity(tmp_path):
    """切换 Embedding 后旧向量被隔离，检索不会因维度不同而报错或混用结果。"""

    assert POSTGRES_TEST_URL is not None
    upgrade_database(POSTGRES_TEST_URL)
    app = JobHuntingApp(tmp_path / "ignored.db", database_url=POSTGRES_TEST_URL)
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


def test_app_deletion_relies_on_postgresql_cascade_for_pgvector_chunks(tmp_path):
    """删除候选人档案时，PostgreSQL 外键应同步删除其 RAG 证据。"""

    assert POSTGRES_TEST_URL is not None
    upgrade_database(POSTGRES_TEST_URL)
    app = JobHuntingApp(tmp_path / "ignored.db", database_url=POSTGRES_TEST_URL)
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
        app.index_rag_long_texts([long_text_id], tmp_path / "ignored-chroma", account_id=account.id)

        deletion = app.delete_candidate_profile(
            candidate_id,
            rag_persist_directory=tmp_path / "ignored-chroma",
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
