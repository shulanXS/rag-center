"""Tests for the bulk KB-deletion path."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from app.core.config import settings
from app.services.chunk_purge_service import purge_kb_all_versions


class _FakeVersion:
    def __init__(self, version: str) -> None:
        self.version = version


@pytest.fixture
def fake_db() -> MagicMock:
    return MagicMock()


def test_purge_kb_all_versions_calls_es_once_per_version(monkeypatch, fake_db):
    """The whole point of this path is to call ES _delete_by_query
    exactly once per index version, not once per document."""
    fake_version_repo = MagicMock()
    fake_version_repo.list_by_kb.return_value = [
        _FakeVersion("v1"),
        _FakeVersion("v2"),
        _FakeVersion("v3"),
    ]
    fake_chunk_repo = MagicMock()
    fake_chunk_repo.count_by_kb_id.return_value = 7
    monkeypatch.setattr(
        "app.services.chunk_purge_service.ChunkRepository",
        lambda *a, **kw: fake_chunk_repo,
    )
    monkeypatch.setattr(
        "app.services.chunk_purge_service.IndexVersionRepository",
        lambda *a, **kw: fake_version_repo,
    )
    monkeypatch.setattr(settings, "keyword_search_provider", "elasticsearch")

    fake_provider = MagicMock()
    fake_provider.delete_by_kb_version = AsyncMock()
    monkeypatch.setattr(
        "app.services.chunk_purge_service.ElasticsearchKeywordSearchProvider",
        lambda *a, **kw: fake_provider,
    )

    removed = asyncio.run(purge_kb_all_versions(fake_db, "t1", "kb-1"))

    # 3 versions => 3 ES round-trips
    assert fake_provider.delete_by_kb_version.await_count == 3
    assert fake_provider.delete_by_kb_version.await_args_list == [
        call(tenant_id="t1", kb_id="kb-1", index_version="v1"),
        call(tenant_id="t1", kb_id="kb-1", index_version="v2"),
        call(tenant_id="t1", kb_id="kb-1", index_version="v3"),
    ]
    # 3 versions => 3 SQL deletes (one per version), regardless of
    # how many documents the KB contains.
    assert fake_chunk_repo.delete_by_kb_version.call_count == 3
    assert removed == 21  # 7 chunks * 3 versions


def test_purge_kb_all_versions_skips_es_when_provider_not_elasticsearch(
    monkeypatch, fake_db
):
    fake_version_repo = MagicMock()
    fake_version_repo.list_by_kb.return_value = [_FakeVersion("v1")]
    fake_chunk_repo = MagicMock()
    monkeypatch.setattr(
        "app.services.chunk_purge_service.ChunkRepository",
        lambda *a, **kw: fake_chunk_repo,
    )
    monkeypatch.setattr(
        "app.services.chunk_purge_service.IndexVersionRepository",
        lambda *a, **kw: fake_version_repo,
    )
    monkeypatch.setattr(settings, "keyword_search_provider", "pgvector")
    fake_provider = MagicMock()
    fake_provider.delete_by_kb_version = AsyncMock()
    monkeypatch.setattr(
        "app.services.chunk_purge_service.ElasticsearchKeywordSearchProvider",
        lambda *a, **kw: fake_provider,
    )

    asyncio.run(purge_kb_all_versions(fake_db, "t1", "kb-1"))
    fake_provider.delete_by_kb_version.assert_not_awaited()


def test_purge_kb_all_versions_continues_when_one_version_es_fails(
    monkeypatch, fake_db
):
    """A single ES error should not block SQL cleanup or the other
    versions' ES deletes."""
    fake_version_repo = MagicMock()
    fake_version_repo.list_by_kb.return_value = [
        _FakeVersion("v1"),
        _FakeVersion("v2"),
    ]
    fake_chunk_repo = MagicMock()
    fake_chunk_repo.count_by_kb_id.return_value = 3
    monkeypatch.setattr(
        "app.services.chunk_purge_service.ChunkRepository",
        lambda *a, **kw: fake_chunk_repo,
    )
    monkeypatch.setattr(
        "app.services.chunk_purge_service.IndexVersionRepository",
        lambda *a, **kw: fake_version_repo,
    )
    monkeypatch.setattr(settings, "keyword_search_provider", "elasticsearch")

    fake_provider = MagicMock()

    async def _fail_on_v1(**kwargs):
        if kwargs.get("index_version") == "v1":
            raise RuntimeError("ES temporarily unavailable")

    fake_provider.delete_by_kb_version = AsyncMock(side_effect=_fail_on_v1)
    monkeypatch.setattr(
        "app.services.chunk_purge_service.ElasticsearchKeywordSearchProvider",
        lambda *a, **kw: fake_provider,
    )

    removed = asyncio.run(purge_kb_all_versions(fake_db, "t1", "kb-1"))
    assert removed == 6  # 3 chunks * 2 versions, both versions removed
    # Both versions still attempted
    assert fake_provider.delete_by_kb_version.await_count == 2
