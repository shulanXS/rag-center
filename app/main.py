from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.exceptions import AppError, ErrorCode
from app.core.logging import configure_logging
from app.schemas.common import error_response

configure_logging()

app = FastAPI(title="RAG Center", version="0.1.0")
app.include_router(api_router)


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(content=error_response(exc.code, exc.msg, exc.data))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        content=error_response(
            ErrorCode.BAD_REQUEST,
            "request validation failed",
            exc.errors(),
        )
    )

