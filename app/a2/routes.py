from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ValidationError

from ..config import get_settings
from ..db import q1
from . import orchestrator as orch
from .sample import SAMPLE_MEETING_DATE, SAMPLE_RULES, SAMPLE_TRANSCRIPT
from .schemas import RulesDoc

router = APIRouter(prefix="/a2")

MAX_TRANSCRIPT_CHARS = 20_000
MAX_RUNS_PER_SESSION = 25  # bounds cost on a public demo URL


def session_id(request: Request, response: Response) -> str:
    """Cookie-bound session. Every A2 query is scoped by this id."""
    sid = orch.ensure_session(request.cookies.get("a2sid"))
    if request.cookies.get("a2sid") != sid:
        response.set_cookie("a2sid", sid, httponly=True, samesite="lax", max_age=7 * 24 * 3600,
                            secure=get_settings().base_url.startswith("https"))
    return sid


class Fault(BaseModel):
    fail_agent: Optional[str] = None       # intake | planner | review
    inject_violation: bool = False


class CreateRun(BaseModel):
    transcript: str
    meeting_date: str
    rules: Optional[dict] = None
    fault: Fault = Fault()


class FactPatch(BaseModel):
    text: Optional[str] = None
    owner: Optional[str] = None
    deadline_text: Optional[str] = None


def _view_or_404(run_id: str, sid: str) -> dict:
    v = orch.run_view(run_id, sid)
    if v is None:
        raise HTTPException(404, "run not found")
    return v


@router.get("", response_class=HTMLResponse)
def page(request: Request, response: Response):
    from ..main import templates
    sid = session_id(request, response)
    resp = templates.TemplateResponse(request, "a2.html", {"sid_short": sid[:6]})
    resp.set_cookie("a2sid", sid, httponly=True, samesite="lax", max_age=7 * 24 * 3600, secure=get_settings().base_url.startswith("https"))
    return resp


@router.get("/api/sample")
def sample():
    return {"transcript": SAMPLE_TRANSCRIPT, "meeting_date": SAMPLE_MEETING_DATE, "rules": SAMPLE_RULES}


@router.get("/api/session")
def session(request: Request, response: Response):
    sid = session_id(request, response)
    return {"session": sid[:6], "runs": [dict(r) for r in orch.list_runs(sid)]}


@router.post("/api/runs")
def create(body: CreateRun, request: Request, response: Response):
    sid = session_id(request, response)
    if len(body.transcript) > MAX_TRANSCRIPT_CHARS or not body.transcript.strip():
        raise HTTPException(422, f"transcript must be 1..{MAX_TRANSCRIPT_CHARS} characters")
    if q1("SELECT COUNT(*) n FROM a2_runs WHERE session_id=?", (sid,))["n"] >= MAX_RUNS_PER_SESSION:
        raise HTTPException(429, "run limit reached for this session; use Reset")
    try:
        rules = RulesDoc.model_validate(body.rules or SAMPLE_RULES)
    except ValidationError as e:
        raise HTTPException(422, f"invalid rules document: {e.errors()[0]['msg']}")
    fault = body.fault.model_dump()
    if not get_settings().a2_enable_fault_injection:
        fault = {}
    if fault.get("fail_agent") not in (None, "", "intake", "planner", "review"):
        raise HTTPException(422, "fail_agent must be intake, planner or review")
    fault["fail_agent"] = fault.get("fail_agent") or None
    try:
        run_id = orch.create_run(sid, body.transcript, body.meeting_date, rules, fault)
    except ValueError:
        raise HTTPException(422, "meeting_date must be YYYY-MM-DD")
    orch.start_async(run_id)
    return {"id": run_id}


@router.get("/api/runs/{run_id}")
def get_run(run_id: str, request: Request, response: Response):
    return _view_or_404(run_id, session_id(request, response))


@router.post("/api/runs/{run_id}/resume")
def resume(run_id: str, request: Request, response: Response):
    sid = session_id(request, response)
    v = _view_or_404(run_id, sid)
    if v["status"] == "running":
        return {"ok": True, "note": "already running"}
    orch.event(run_id, "info", "Resume requested: completed steps will be reused, only the failed/missing step runs.")
    orch.start_async(run_id)
    return {"ok": True}


@router.post("/api/runs/{run_id}/facts/{fact_id}")
def correct(run_id: str, fact_id: str, patch: FactPatch, request: Request, response: Response):
    sid = session_id(request, response)
    _view_or_404(run_id, sid)
    try:
        v = orch.correct_fact(run_id, sid, fact_id, patch.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e))
    orch.start_async(run_id)
    return {"context_version": v}


@router.post("/api/reset")
def reset(request: Request, response: Response):
    sid = session_id(request, response)
    return {"deleted_runs": orch.reset_session(sid)}
