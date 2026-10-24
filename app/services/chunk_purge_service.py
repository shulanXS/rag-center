from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.providers.keyword_search.elasticsearch import ElasticsearchKeywordSearchProvider
from app.repositories.chunk_repository import ChunkRepository
from app.repositories.index_version_repository import IndexVersionRepository

logger = get_logger(__name__)


async def purge_document_chunks(
    db: Session, document_id: str, *, index_version: str | None = None
) -> None:
    ChunkRepository(db).delete_by_document_id(document_id, index_version=index_version)
    if settings.keyword_search_provider == "elasticsearch":
        await ElasticsearchKeywordSearchProvider().delete_by_document_id(
            document_id,
            index_version=index_version,
        )
    logger.info(
        "BUSINESS_NODE | purge_document_chunks_success | document_id=%s | "
        "index_version=%s | keyword_search=%s",
        document_id,
        index_version,
        settings.keyword_search_provider,
    )


async def purge_kb_all_versions(
    db: Session,
    tenant_id: str,
    kb_id: str,
) -> int:
    """Delete every chunk (and the matching ES documents) for one KB
    across all known index versions.

    The previous implementation looped over each document and called
    ``purge_document_chunks`` per document, which produced one
    `_delete_by_query` per document on the ES side. For large KBs
    that was O(N) round-trips. This path collapses the work to a
    single ES `_delete_by_query` per index version, plus a single
    batched chunk delete on the SQL side per version.

    Returns the number of chunk rows removed.
    """
    chunk_repo = ChunkRepository(db)
    version_repo = IndexVersionRepository(db)
    versions = version_repo.list_by_kb(tenant_id, kb_id)
    version_strs = [v.version for v in versions]

    total_chunks = 0
    for version in version_strs:
        # Pre-count so the operator-visible metric is accurate even
        # though we issue a single DELETE.
        total_chunks += chunk_repo.count_by_kb_id(tenant_id, kb_id, index_version=version)
        chunk_repo.delete_by_kb_version(tenant_id, kb_id, version)

    if settings.keyword_search_provider == "elasticsearch" and version_strs:
        # One ES round-trip per version instead of one per document.
        provider = ElasticsearchKeywordSearchProvider()
        for version in version_strs:
            try:
                await provider.delete_by_kb_version(
                    tenant_id=tenant_id, kb_id=kb_id, index_version=version
                )
            except Exception:
                logger.exception(
                    "KB_DELETE_ES_FAILED | tenant_id=%s | kb_id=%s | version=%s",
                    tenant_id,
                    kb_id,
                    version,
                )
                # Continue with the next version — a single ES error
                # should not stop the SQL cleanup, and the operator can
                # re-run the ES cleanup if needed.
    logger.info(
        "BUSINESS_NODE | purge_kb_all_versions_success | tenant_id=%s | "
        "kb_id=%s | versions=%s | chunk_count=%s",
        tenant_id,
        kb_id,
        version_strs,
        total_chunks,
    )
    return total_chunks
