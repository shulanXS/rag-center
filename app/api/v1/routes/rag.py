from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_tenant
from app.core.auth import TenantContext
from app.core.exceptions import AppError, ErrorCode
from app.core.http_clients import ExternalHttpClients, get_external_http_clients
from app.db.session import get_db
from app.models.research_task import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
)
from app.schemas.common import success_response
from app.schemas.feedback import FeedbackRequest
from app.schemas.rag import RetrieveRequest
from app.schemas.research import ResearchRequest
from app.services.feedback_service import FeedbackService
from app.services.rag_service import RagService
from app.services.research_task_service import ResearchTaskService

router = APIRouter(prefix="/rag", tags=["rag"])


@router.post("/retrieve")
async def retrieve(
    request: RetrieveRequest,
    db: Session = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant),
    http_clients: ExternalHttpClients = Depends(get_external_http_clients),
) -> dict:
    data = await RagService(db, http_clients=http_clients).retrieve(
        tenant.tenant_id,
        request,
    )
    return success_response(data.model_dump(mode="json"))


@router.post("/research")
async def submit_research(
    request: ResearchRequest,
    db: Session = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant),
    x_eval_allow_supplement: bool | None = Header(
        default=None,
        alias="X-Eval-Allow-Supplement",
        include_in_schema=False,
    ),
) -> dict:
    """
    Submit a Research request. The actual research pipeline is run by a
    Celery worker so this endpoint returns immediately with a
    ``research_id`` that the client can poll via ``GET /rag/research/{id}``.
    """
    allow_supplement = x_eval_allow_supplement is not False
    service = ResearchTaskService(db)
    task = service.submit(
        tenant.tenant_id,
        request,
        allow_supplement=allow_supplement,
    )
    return success_response(
        {
            "research_id": task.id,
            "status": STATUS_PENDING,
        }
    )


@router.get("/research/{research_id}")
async def get_research_result(
    research_id: str,
    db: Session = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict:
    """
    Poll a previously-submitted Research request. The endpoint returns
    the full ``ResearchData`` payload once the Celery worker has
    finished, or a small status envelope while the request is still in
    flight. Failed tasks surface as a ``RESEARCH_FAILED`` AppError.
    """
    service = ResearchTaskService(db)
    task = service.get(tenant.tenant_id, research_id)

    if task.status == STATUS_PENDING or task.status == STATUS_RUNNING:
        return success_response(
            {
                "research_id": task.id,
                "status": task.status,
                "progress": None,
            }
        )
    if task.status == STATUS_COMPLETED:
        # task.result is the ResearchData JSON written by the executor
        return success_response(task.result or {})
    if task.status == STATUS_FAILED:
        raise AppError(
            ErrorCode.RESEARCH_FAILED,
            msg="Research 任务执行失败",
            internal_msg=task.error or "research task failed",
            context={"research_id": task.id, "tenant_id": task.tenant_id},
        )
    # Defensive: unknown status values are treated as failures so the
    # client gets a clear signal rather than a silent hang.
    raise AppError(
        ErrorCode.RESEARCH_FAILED,
        msg=f"Research task is in unknown status: {task.status}",
        context={"research_id": task.id, "status": task.status},
    )


@router.post("/feedback")
def submit_feedback(
    request: FeedbackRequest,
    db: Session = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict:
    data = FeedbackService(db).submit_feedback(tenant.tenant_id, request)
    return success_response(data.model_dump(mode="json"))
