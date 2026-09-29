from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.memory_schemas import RecallRequest, ReflectRequest, RetainRequest
from app.services.hindsight import HindsightAdapter, HindsightError, get_hindsight_adapter

memory_router = APIRouter(prefix="/api/memory", tags=["Hindsight memory"])


async def hindsight_error_handler(_: Request, exc: HindsightError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": {"code": exc.code, "message": exc.message}},
    )


@memory_router.post("/retain")
async def retain_memory(
    payload: RetainRequest,
    hindsight: HindsightAdapter = Depends(get_hindsight_adapter),
) -> dict[str, Any]:
    return await hindsight.retain(
        content=payload.content,
        context=payload.context,
        document_id=payload.document_id,
        timestamp=payload.timestamp.isoformat() if payload.timestamp else None,
    )


@memory_router.post("/recall")
async def recall_memory(
    payload: RecallRequest,
    hindsight: HindsightAdapter = Depends(get_hindsight_adapter),
) -> dict[str, Any]:
    return await hindsight.recall(
        query=payload.query,
        types=payload.types,
        budget=payload.budget,
        max_tokens=payload.max_tokens,
    )


@memory_router.post("/reflect")
async def reflect_on_memory(
    payload: ReflectRequest,
    hindsight: HindsightAdapter = Depends(get_hindsight_adapter),
) -> dict[str, Any]:
    return await hindsight.reflect(
        query=payload.query,
        budget=payload.budget,
        max_tokens=payload.max_tokens,
        include_facts=payload.include_facts,
    )
