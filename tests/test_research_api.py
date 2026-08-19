import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.v1.routes import rag as rag_routes
from app.schemas.research import ResearchRequest


def test_research_request_reuses_kb_normalization_and_defaults_to_quality():
    request = ResearchRequest(
        kb_ids=[" kb-1 ", "kb-1", "kb-2"],
        user_id="user",
        query=" 退款规则 ",
    )
    assert request.kb_id == "kb-1"
    assert request.kb_ids == ["kb-1", "kb-2"]
    assert request.query == "退款规则"
    assert request.profile == "quality"


@pytest.mark.parametrize(
    "field,value",
    [
        ("top_k", 21),
        ("max_rounds", 9),
        ("tools", ["browser"]),
        ("web_search", True),
    ],
)
def test_research_request_rejects_limits_and_external_tool_controls(field, value):
    payload = {"kb_id": "kb", "user_id": "user", "query": "问题", field: value}
    with pytest.raises(ValidationError):
        ResearchRequest(**payload)


def test_research_route_uses_dedicated_service(monkeypatch):
    expected = SimpleNamespace(model_dump=lambda **kwargs: {"query": "问题"})

    class FakeService:
        def __init__(self, db):
            self.db = db

        async def research(self, tenant_id, request):
            assert tenant_id == "tenant"
            assert request.profile == "quality"
            return expected

    monkeypatch.setattr(rag_routes, "ResearchService", FakeService)
    response = asyncio.run(
        rag_routes.research(
            ResearchRequest(kb_id="kb", user_id="user", query="问题"),
            db=SimpleNamespace(),
            tenant=SimpleNamespace(tenant_id="tenant"),
        )
    )
    assert response["code"] == 0
    assert response["data"] == {"query": "问题"}
