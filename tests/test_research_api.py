import asyncio
from types import SimpleNamespace

import pytest

from app.api.v1.routes import rag as rag_routes
from app.core.exceptions import AppError, ErrorCode
from app.models.research_task import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
)
from app.schemas.research import ResearchRequest


def test_research_request_reuses_kb_normalization_and_has_no_retrieval_controls():
    request = ResearchRequest(
        kb_ids=[" kb-1 ", "kb-1", "kb-2"],
        user_id="user",
        query=" 退款规则 ",
    )
    assert request.kb_id == "kb-1"
    assert request.kb_ids == ["kb-1", "kb-2"]
    assert request.query == "退款规则"
    assert not hasattr(request, "profile")
    assert not hasattr(request, "top_k")


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile", "speed"),
        ("index_version", "v1"),
        ("retrieval_options", {"mode": "vector"}),
        ("rerank_options", {"enabled": False}),
        ("top_k", 10),
        ("max_rounds", 9),
        ("tools", ["browser"]),
        ("web_search", True),
    ],
)
def test_research_request_rejects_limits_and_external_tool_controls(field, value):
    payload = {"kb_id": "kb", "user_id": "user", "query": "问题", field: value}
    with pytest.raises(Exception):  # ValidationError or similar
        ResearchRequest(**payload)


def test_submit_research_creates_task_and_dispatches(monkeypatch):
    """POST /rag/research must persist a pending task and dispatch the
    Celery job — it must never execute the long Research pipeline on
    the request thread."""
    captured: dict = {}
    fake_task = SimpleNamespace(
        id="research-123",
        status=STATUS_PENDING,
        tenant_id="tenant",
    )

    class FakeTaskService:
        def __init__(self, db):
            self.db = db
            assert db is fake_db

        def submit(self, tenant_id, request, *, allow_supplement=True):
            captured["tenant_id"] = tenant_id
            captured["request"] = request
            captured["allow_supplement"] = allow_supplement
            return fake_task

    fake_db = SimpleNamespace()

    # Prevent the real Celery task from being scheduled.
    monkeypatch.setattr(
        "app.services.research_task_service.submit_research_task",
        SimpleNamespace(delay=lambda task_id: None),
    )
    monkeypatch.setattr(rag_routes, "ResearchTaskService", FakeTaskService)

    response = asyncio.run(
        rag_routes.submit_research(
            ResearchRequest(kb_id="kb", user_id="user", query="问题"),
            db=fake_db,
            tenant=SimpleNamespace(tenant_id="tenant"),
        )
    )

    assert response["code"] == 0
    assert response["data"] == {"research_id": "research-123", "status": STATUS_PENDING}
    assert captured["tenant_id"] == "tenant"
    assert captured["request"].query == "问题"
    assert captured["allow_supplement"] is True


def test_submit_research_respects_eval_header(monkeypatch):
    """The hidden X-Eval-Allow-Supplement header should propagate to
    the service. We only test the False path because the default (None)
    is mapped to allow_supplement=True by the route."""
    captured: dict = {}

    class FakeTaskService:
        def __init__(self, db):
            self.db = db

        def submit(self, tenant_id, request, *, allow_supplement=True):
            captured["allow_supplement"] = allow_supplement
            return SimpleNamespace(id="x", status=STATUS_PENDING)

    monkeypatch.setattr(rag_routes, "ResearchTaskService", FakeTaskService)
    monkeypatch.setattr(
        "app.services.research_task_service.submit_research_task",
        SimpleNamespace(delay=lambda x: None),
    )

    asyncio.run(
        rag_routes.submit_research(
            ResearchRequest(kb_id="kb", user_id="user", query="问题"),
            db=SimpleNamespace(),
            tenant=SimpleNamespace(tenant_id="tenant"),
            x_eval_allow_supplement=False,
        )
    )
    assert captured["allow_supplement"] is False


@pytest.mark.parametrize(
    "status,expected_status",
    [
        (STATUS_PENDING, STATUS_PENDING),
        (STATUS_RUNNING, STATUS_RUNNING),
    ],
)
def test_poll_endpoint_returns_status_envelope_while_in_flight(
    monkeypatch, status, expected_status
):
    """While the Celery worker hasn't finished, the poll endpoint
    must return only a small status envelope, not the full payload."""
    captured: dict = {}

    class FakeTaskService:
        def __init__(self, db):
            self.db = db

        def get(self, tenant_id, research_id):
            captured["tenant_id"] = tenant_id
            captured["research_id"] = research_id
            return SimpleNamespace(
                id=research_id,
                status=status,
                result=None,
                error=None,
                tenant_id=tenant_id,
            )

    monkeypatch.setattr(rag_routes, "ResearchTaskService", FakeTaskService)
    response = asyncio.run(
        rag_routes.get_research_result(
            "research-abc",
            db=SimpleNamespace(),
            tenant=SimpleNamespace(tenant_id="tenant"),
        )
    )
    assert response["code"] == 0
    assert response["data"] == {
        "research_id": "research-abc",
        "status": expected_status,
        "progress": None,
    }
    assert captured == {"tenant_id": "tenant", "research_id": "research-abc"}


def test_poll_endpoint_returns_full_result_when_completed(monkeypatch):
    completed_payload = {"query": "问题", "answer": "42"}

    class FakeTaskService:
        def __init__(self, db):
            self.db = db

        def get(self, tenant_id, research_id):
            return SimpleNamespace(
                id=research_id,
                status=STATUS_COMPLETED,
                result=completed_payload,
                error=None,
                tenant_id=tenant_id,
            )

    monkeypatch.setattr(rag_routes, "ResearchTaskService", FakeTaskService)
    response = asyncio.run(
        rag_routes.get_research_result(
            "research-abc",
            db=SimpleNamespace(),
            tenant=SimpleNamespace(tenant_id="tenant"),
        )
    )
    assert response["code"] == 0
    assert response["data"] == completed_payload


def test_poll_endpoint_raises_research_failed_when_task_failed(monkeypatch):
    class FakeTaskService:
        def __init__(self, db):
            self.db = db

        def get(self, tenant_id, research_id):
            return SimpleNamespace(
                id=research_id,
                status=STATUS_FAILED,
                result=None,
                error="LLM timeout after 90s",
                tenant_id=tenant_id,
            )

    monkeypatch.setattr(rag_routes, "ResearchTaskService", FakeTaskService)

    with pytest.raises(AppError) as exc_info:
        asyncio.run(
            rag_routes.get_research_result(
                "research-abc",
                db=SimpleNamespace(),
                tenant=SimpleNamespace(tenant_id="tenant"),
            )
        )
    assert exc_info.value.code == ErrorCode.RESEARCH_FAILED.code
    assert "LLM timeout" in (exc_info.value.internal_msg or "")


def test_poll_endpoint_raises_not_found_when_task_missing(monkeypatch):
    class FakeTaskService:
        def __init__(self, db):
            self.db = db

        def get(self, tenant_id, research_id):
            raise AppError(
                ErrorCode.NOT_FOUND,
                msg="Research task not found",
                context={"research_id": research_id},
            )

    monkeypatch.setattr(rag_routes, "ResearchTaskService", FakeTaskService)
    with pytest.raises(AppError) as exc_info:
        asyncio.run(
            rag_routes.get_research_result(
                "missing",
                db=SimpleNamespace(),
                tenant=SimpleNamespace(tenant_id="tenant"),
            )
        )
    assert exc_info.value.code == ErrorCode.NOT_FOUND.code


def test_research_task_service_dispatches_celery(monkeypatch):
    """The service must persist the task, commit, then dispatch the
    Celery job — so the request handler can return immediately."""
    from app.services.research_task_service import ResearchTaskService

    dispatched_ids: list[str] = []
    persisted_ids: list[str] = []

    class FakeRepo:
        def create(self, task):
            persisted_ids.append(task.id)
            return task

    class FakeDB:
        def commit(self):
            pass

    monkeypatch.setattr(
        "app.services.research_task_service.submit_research_task",
        SimpleNamespace(delay=lambda task_id: dispatched_ids.append(task_id)),
    )

    service = ResearchTaskService(FakeDB(), repository=FakeRepo())
    task = service.submit(
        "t1",
        ResearchRequest(kb_id="kb1", user_id="u1", query="q"),
    )
    assert task.tenant_id == "t1"
    assert task.user_id == "u1"
    assert task.query == "q"
    assert task.kb_ids == ["kb1"]
    assert task.status == "pending"
    assert len(persisted_ids) == 1
    assert persisted_ids[0] == task.id
    assert dispatched_ids == [task.id]
