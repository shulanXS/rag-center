from app.core.index_versions import LEGACY_INDEX_VERSION
from app.providers.keyword_search.elasticsearch import _build_search_payload


def test_legacy_search_requires_meaningful_term_overlap():
    payload = _build_search_payload(
        query="SC编号、CCC认证和ISBN分别对应哪些商品类目？",
        filters=[{"term": {"index_version": LEGACY_INDEX_VERSION}}],
        index_version=LEGACY_INDEX_VERSION,
        top_k=5,
    )

    multi_match = payload["query"]["bool"]["must"]["multi_match"]
    assert multi_match == {
        "query": "SC编号、CCC认证和ISBN分别对应哪些商品类目？",
        "fields": ["metadata.heading_path^2", "content^2", "title"],
        "type": "cross_fields",
        "minimum_should_match": "50%",
    }
    assert payload["size"] == 5


def test_contextual_index_keeps_retrieval_text_search():
    payload = _build_search_payload(
        query="测试问题",
        filters=[],
        index_version="contextual",
        top_k=10,
    )

    multi_match = payload["query"]["bool"]["must"]["multi_match"]
    assert multi_match == {
        "query": "测试问题",
        "fields": ["retrieval_text"],
    }
