from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Incident
from app.config import Settings, get_settings
from app.schemas import IncidentCreate, IncidentInvestigationRequest, IncidentInvestigationResponse, IncidentRead, IncidentResolve, IncidentResolveResponse
from app.services.hindsight import HindsightAdapter, HindsightError, get_hindsight_adapter
from app.services.incident_agent import investigate_incident
from app.services.llm import LLMError

router = APIRouter(prefix="/api", tags=["incident metadata"])


@router.get("/health")
def health_check(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(select(1))
    return {"status": "ok", "database": "ok"}


@router.get("/incidents", response_model=list[IncidentRead])
def list_incidents(db: Session = Depends(get_db)) -> list[Incident]:
    return list(db.scalars(select(Incident).order_by(Incident.created_at.desc())))


@router.post("/incidents", response_model=IncidentRead, status_code=status.HTTP_201_CREATED)
def create_incident(payload: IncidentCreate, db: Session = Depends(get_db)) -> Incident:
    incident = Incident(title=payload.title, service=payload.service)
    db.add(incident)
    db.commit()
    db.refresh(incident)
    return incident


@router.post("/incidents/{incident_id}/investigate", response_model=IncidentInvestigationResponse)
async def investigate_existing_incident(
    incident_id: int,
    payload: IncidentInvestigationRequest,
    db: Session = Depends(get_db),
    hindsight: HindsightAdapter = Depends(get_hindsight_adapter),
    settings: Settings = Depends(get_settings),
) -> IncidentInvestigationResponse:
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    try:
        return await investigate_incident(
            incident=incident,
            evidence=payload.evidence,
            hindsight=hindsight,
            settings=settings,
        )
    except HindsightError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except LLMError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/incidents/{incident_id}/resolve")
async def resolve_incident(
    incident_id: int,
    payload: IncidentResolve,
    db: Session = Depends(get_db),
    hindsight: HindsightAdapter = Depends(get_hindsight_adapter),
) -> IncidentResolveResponse:
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")

    retained = True
    if incident.status != "resolved":
        resolved_at = datetime.now(timezone.utc)
        await hindsight.retain(
            content=(
                f"Incident {incident.id} was resolved. Title: {incident.title}. Service: {incident.service}. "
                f"What happened / evidence: {payload.evidence.strip() or 'No additional evidence provided.'} "
                f"Resolution / outcome: {payload.outcome.strip()} Resolved at: {resolved_at.isoformat()}."
            ),
            context=f"Resolved production incident for {incident.service}; incident_id={incident.id}",
            document_id=f"incident-{incident.id}",
            timestamp=resolved_at.isoformat(),
        )
        incident.status = "resolved"
        db.commit()
        db.refresh(incident)
    return IncidentResolveResponse(
        success=True,
        incident=IncidentRead.model_validate(incident),
        retained=bool(retained),
        outcome=payload.outcome.strip(),
    )
