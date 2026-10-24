from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.core.index_versions import DEFAULT_INDEX_VERSION
from app.core.logging import get_logger
from app.models.index_version import KnowledgeBaseIndexVersion
from app.providers.keyword_search.elasticsearch import ElasticsearchKeywordSearchProvider
from app.repositories.chunk_relation_repository import ChunkRelationRepository
from app.repositories.chunk_repository import ChunkRepository
from app.repositories.document_repository import DocumentRepository
from app.repositories.index_version_repository import IndexVersionRepository
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.schemas.knowledge_base import IndexVersionData, RebuildIndexData
from app.services.indexing_service import create_indexing_service
from app.services.reference_relation_service import ReferenceRelationService
from app.utils.id_generator import generate_uuid
from app.utils.status import DocumentStatus

logger = get_logger(__name__)
VERSION_RE = re.compile(r"^v(\d+)$")


def utc_now() -> datetime:
    return datetime.now(UTC)


class IndexVersionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.knowledge_base_repository = KnowledgeBaseRepository(db)
        self.version_repository = IndexVersionRepository(db)
        self.document_repository = DocumentRepository(db)
        self.chunk_repository = ChunkRepository(db)
        self.relation_repository = ChunkRelationRepository(db)

    def create_initial(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        version: str = DEFAULT_INDEX_VERSION,
    ) -> None:
        self.version_repository.create(
            KnowledgeBaseIndexVersion(
                id=generate_uuid(),
                tenant_id=tenant_id,
                kb_id=kb_id,
                version=version,
                status="active",
                document_count=0,
                chunk_count=0,
                completed_at=utc_now(),
                activated_at=utc_now(),
            )
        )

    def list_versions(self, tenant_id: str, kb_id: str) -> list[IndexVersionData]:
        self._get_kb_or_raise(tenant_id, kb_id)
        return [
            self._to_data(item) for item in self.version_repository.list_by_kb(tenant_id, kb_id)
        ]

    def queue_rebuild(self, tenant_id: str, kb_id: str) -> RebuildIndexData:
        data = self.start_rebuild(tenant_id, kb_id)

        # Dispatch the chained rebuild (one batch task per ~25 docs
        # plus a finalisation task). The legacy single-shot task is
        # retained for tests and environments that have not enabled
        # the batch path.
        self.rebuild_dispatch(tenant_id, kb_id, data.version)
        return data

    def start_rebuild(self, tenant_id: str, kb_id: str) -> RebuildIndexData:
        self._get_kb_or_raise(tenant_id, kb_id)
        if self.version_repository.get_building(tenant_id, kb_id) is not None:
            raise AppError(
                ErrorCode.SYSTEM_BUSY,
                msg="该知识库已有索引版本正在构建",
                context={"tenant_id": tenant_id, "kb_id": kb_id},
            )
        version = self._next_version(tenant_id, kb_id)
        item = self.version_repository.create(
            KnowledgeBaseIndexVersion(
                id=generate_uuid(),
                tenant_id=tenant_id,
                kb_id=kb_id,
                version=version,
                status="building",
                document_count=0,
                chunk_count=0,
            )
        )
        self.db.commit()
        return RebuildIndexData(kb_id=kb_id, version=item.version, status=item.status)

    async def rebuild(self, tenant_id: str, kb_id: str, version: str) -> None:
        item = self.version_repository.get(tenant_id, kb_id, version)
        if item is None or item.status != "building":
            return
        documents = self.document_repository.list_by_kb_id(kb_id)
        documents = [
            document
            for document in documents
            if document.tenant_id == tenant_id
            and document.status == DocumentStatus.SUCCESS
            and document.content.strip()
        ]
        # In-process rebuild: used by tests and the legacy single-shot
        # path. Production uses ``rebuild_dispatch`` which fans the
        # work out to Celery sub-tasks.
        try:
            indexing_service = create_indexing_service(self.db)
            for document in documents:
                await indexing_service.index_document_version(document, index_version=version)
            relation_count = ReferenceRelationService(self.db).rebuild(
                tenant_id=tenant_id,
                kb_id=kb_id,
                index_version=version,
            )
            item.document_count = len(documents)
            item.chunk_count = self.chunk_repository.count_by_kb_id(
                tenant_id, kb_id, index_version=version
            )
            item.status = "ready"
            item.completed_at = utc_now()
            item.error_message = None
            self.db.commit()
            logger.info(
                "INDEX_VERSION_READY | tenant_id=%s | kb_id=%s | version=%s | "
                "document_count=%s | chunk_count=%s | relation_count=%s",
                tenant_id,
                kb_id,
                version,
                item.document_count,
                item.chunk_count,
                relation_count,
            )
        except Exception as exc:
            self._mark_rebuild_failed(tenant_id, kb_id, version, str(exc))
            raise

    def rebuild_dispatch(
        self,
        tenant_id: str,
        kb_id: str,
        version: str,
        *,
        batch_size: int = 25,
    ) -> int:
        """
        Fan out the rebuild work to Celery sub-tasks. Returns the
        number of sub-tasks dispatched.

        The batch tasks are chained so the version is only flipped to
        ``ready`` after every batch has been processed. Sub-tasks are
        dispatched in order so the last one to finish is the one that
        triggers the finalisation step.
        """
        from app.tasks.indexing import (
            finalize_index_version_task,
            index_kb_batch_task,
        )

        documents = self.document_repository.list_by_kb_id(kb_id)
        documents = [
            document
            for document in documents
            if document.tenant_id == tenant_id
            and document.status == DocumentStatus.SUCCESS
            and document.content.strip()
        ]
        batches = [
            documents[i : i + batch_size]
            for i in range(0, len(documents), batch_size)
        ]
        # No documents: skip straight to finalisation
        if not batches:
            finalize_index_version_task.delay(tenant_id, kb_id, version)
            return 0

        # Build the chain: batch[0] -> batch[1] -> ... -> batch[N-1] ->
        # finalisation. The chain is applied asynchronously so all
        # sub-tasks land on the broker in a single round-trip.
        chain = index_kb_batch_task.s(
            tenant_id, kb_id, version, 0, [d.id for d in batches[0]]
        )
        for batch_idx, batch in enumerate(batches[1:], start=1):
            chain = chain | index_kb_batch_task.s(
                tenant_id, kb_id, version, batch_idx, [d.id for d in batch]
            )
        chain = chain | finalize_index_version_task.s(tenant_id, kb_id, version)
        chain.apply_async()
        return len(batches)

    def _mark_rebuild_failed(
        self,
        tenant_id: str,
        kb_id: str,
        version: str,
        error: str,
    ) -> None:
        """Roll back partial chunks/relations and mark the version as
        failed. Used by both the in-process and the batched rebuild
        paths."""
        try:
            self.chunk_repository.delete_by_kb_version(tenant_id, kb_id, version)
            self.relation_repository.delete_by_kb_version(tenant_id, kb_id, version)
            self.db.commit()
        except Exception:
            self.db.rollback()
            logger.exception(
                "INDEX_VERSION_CLEANUP_FAILED | tenant_id=%s | kb_id=%s | version=%s",
                tenant_id,
                kb_id,
                version,
            )
        if settings.keyword_search_provider == "elasticsearch":
            try:
                # Run the cleanup in a fresh loop because we may be
                # inside an async context already.
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = None
                if loop is not None and loop.is_running():
                    # Schedule the coroutine for the next tick.
                    loop.create_task(
                        ElasticsearchKeywordSearchProvider().delete_by_kb_version(
                            tenant_id=tenant_id,
                            kb_id=kb_id,
                            index_version=version,
                        )
                    )
                else:
                    asyncio.run(
                        ElasticsearchKeywordSearchProvider().delete_by_kb_version(
                            tenant_id=tenant_id,
                            kb_id=kb_id,
                            index_version=version,
                        )
                    )
            except Exception:
                logger.exception(
                    "INDEX_VERSION_ES_CLEANUP_FAILED | tenant_id=%s | kb_id=%s | version=%s",
                    tenant_id,
                    kb_id,
                    version,
                )
        item = self.version_repository.get(tenant_id, kb_id, version)
        if item is not None:
            item.status = "failed"
            item.error_message = error[:2000]
            item.completed_at = utc_now()
            self.db.commit()

    def activate(self, tenant_id: str, kb_id: str, version: str) -> IndexVersionData:
        knowledge_base = self._get_kb_or_raise(tenant_id, kb_id)
        target = self.version_repository.get(tenant_id, kb_id, version)
        if target is None:
            raise AppError(ErrorCode.NOT_FOUND, msg="索引版本不存在")
        if target.status == "active":
            return self._to_data(target)
        if target.status not in {"ready", "archived"}:
            raise AppError(
                ErrorCode.PARAM_ERROR,
                msg="只有已就绪或已归档的索引版本可以激活",
                context={"status": target.status, "version": version},
            )

        current = self.version_repository.get(tenant_id, kb_id, knowledge_base.active_index_version)
        if current is not None and current.status == "active":
            current.status = "archived"
            self.db.flush()
        target.status = "active"
        target.activated_at = utc_now()
        knowledge_base.active_index_version = version
        self.db.commit()
        self.db.refresh(target)
        return self._to_data(target)

    async def delete_version(self, tenant_id: str, kb_id: str, version: str) -> None:
        knowledge_base = self._get_kb_or_raise(tenant_id, kb_id)
        item = self.version_repository.get(tenant_id, kb_id, version)
        if item is None:
            raise AppError(ErrorCode.NOT_FOUND, msg="索引版本不存在")
        if knowledge_base.active_index_version == version or item.status == "active":
            raise AppError(ErrorCode.PARAM_ERROR, msg="活动索引版本不能删除")
        if item.status == "building":
            raise AppError(ErrorCode.SYSTEM_BUSY, msg="正在构建的索引版本不能删除")

        self.relation_repository.delete_by_kb_version(tenant_id, kb_id, version)
        self.chunk_repository.delete_by_kb_version(tenant_id, kb_id, version)
        self.version_repository.delete(item)
        if settings.keyword_search_provider == "elasticsearch":
            await ElasticsearchKeywordSearchProvider().delete_by_kb_version(
                tenant_id=tenant_id,
                kb_id=kb_id,
                index_version=version,
            )
        self.db.commit()

    def ensure_not_building(self, tenant_id: str, kb_id: str) -> None:
        if self.version_repository.get_building(tenant_id, kb_id) is not None:
            raise AppError(
                ErrorCode.SYSTEM_BUSY,
                msg="知识库正在构建新索引，暂时不能修改文档",
                context={"tenant_id": tenant_id, "kb_id": kb_id},
            )

    def _next_version(self, tenant_id: str, kb_id: str) -> str:
        numbers = []
        for item in self.version_repository.list_by_kb(tenant_id, kb_id):
            match = VERSION_RE.match(item.version)
            if match:
                numbers.append(int(match.group(1)))
        return f"v{max(numbers, default=1) + 1}"

    def _get_kb_or_raise(self, tenant_id: str, kb_id: str):
        knowledge_base = self.knowledge_base_repository.get_by_id_and_tenant(kb_id, tenant_id)
        if knowledge_base is None:
            raise AppError(
                ErrorCode.KNOWLEDGE_BASE_NOT_FOUND,
                context={"tenant_id": tenant_id, "kb_id": kb_id},
            )
        return knowledge_base

    @staticmethod
    def _to_data(item: KnowledgeBaseIndexVersion) -> IndexVersionData:
        return IndexVersionData(
            version=item.version,
            status=item.status,
            document_count=item.document_count,
            chunk_count=item.chunk_count,
            error_message=item.error_message,
            created_at=item.created_at,
            completed_at=item.completed_at,
            activated_at=item.activated_at,
        )
