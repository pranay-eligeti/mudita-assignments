"""Orchestration for the three-agent coordinator.

Guarantees
* Every agent output and every handoff is persisted (a2_steps / a2_handoffs) under a run id and context version.
* A step's identity is a hash of (prompt version, model, agent, full input). Same input -> the stored output is
  reused, so resuming after a failure or re-running after an edit never repeats completed work, and a changed
  fact changes the hash of everything downstream so stale context cannot be reused.
* Handoffs are validated by constructing the receiving agent's input model before any model call.
* Review <-> Planner loop is bounded (A2_MAX_REVIEW_ROUNDS); unresolved issues are surfaced, never hidden.
* All reads/writes are scoped to a session id; a run is invisible to other sessions.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import date, timedelta
from typing import Callable, Optional, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import get_settings
from ..db import jdump, jload, now_iso, q, q1, run as sql, tx
from ..llm import LLMError, SimulatedModelFailure
from . import agents
from .schemas import (
    Context, IntakeInput, IntakeOutput, Issue, PlannerInput, PlanOutput, ReviewInput, ReviewOutput, RulesDoc,
)
from .validators import build_context, dedupe_issues, static_check

T = TypeVar("T", bound=BaseModel)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class StepFailed(Exception):
    def __init__(self, agent: str, message: str, simulated: bool):
        super().__init__(message)
        self.agent, self.simulated = agent, simulated


class HandoffError(Exception):
    pass


class Superseded(Exception):
    """The user changed a fact while this execution was running; abandon it, the next execution uses the new version."""


def _lock(run_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(run_id, threading.Lock())


# ------------------------------------------------------------------ sessions & runs


def new_session_id() -> str:
    return secrets.token_urlsafe(24)


def ensure_session(sid: Optional[str]) -> str:
    if sid and q1("SELECT 1 FROM a2_sessions WHERE id=?", (sid,)):
        return sid
    sid = new_session_id()
    sql("INSERT INTO a2_sessions(id, created_at) VALUES(?,?)", (sid, now_iso()))
    return sid


def create_run(session_id: str, transcript: str, meeting_date: str, rules: RulesDoc, fault: dict | None = None) -> str:
    date.fromisoformat(meeting_date)  # validate
    run_id = "run_" + secrets.token_hex(6)
    sql(
        "INSERT INTO a2_runs(id,session_id,transcript,meeting_date,rules_json,context_version,status,fault_json,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (run_id, session_id, transcript, meeting_date, jdump(rules.model_dump(mode="json")), 1, "pending", jdump(fault or {}), now_iso(), now_iso()),
    )
    event(run_id, "info", "Run created. Context version 1.")
    return run_id


def get_run(run_id: str, session_id: str):
    """Session-scoped fetch. Returns None for another session's run, indistinguishable from 'not found'."""
    return q1("SELECT * FROM a2_runs WHERE id=? AND session_id=?", (run_id, session_id))


def list_runs(session_id: str):
    return q("SELECT id,status,context_version,created_at FROM a2_runs WHERE session_id=? ORDER BY created_at DESC", (session_id,))


def reset_session(session_id: str) -> int:
    runs = [r["id"] for r in q("SELECT id FROM a2_runs WHERE session_id=?", (session_id,))]
    with tx() as c:
        for rid in runs:
            for t in ("a2_events", "a2_handoffs", "a2_steps", "a2_fact_overrides"):
                c.execute(f"DELETE FROM {t} WHERE run_id=?", (rid,))
            c.execute("DELETE FROM llm_calls WHERE scope=?", (f"a2:run:{rid}",))
            c.execute("DELETE FROM a2_runs WHERE id=?", (rid,))
    return len(runs)


def event(run_id: str, kind: str, text: str) -> None:
    sql("INSERT INTO a2_events(run_id,kind,text,created_at) VALUES(?,?,?,?)", (run_id, kind, text, now_iso()))


def set_status(run_id: str, status: str, error: str | None = None, final: dict | None = None) -> None:
    sql("UPDATE a2_runs SET status=?, error=?, final_json=COALESCE(?, final_json), updated_at=? WHERE id=?",
        (status, error, jdump(final) if final is not None else None, now_iso(), run_id))


def recover_interrupted() -> int:
    """After a process restart, a 'running' run has no worker. Mark it failed so the user can resume it."""
    rows = q("SELECT id FROM a2_runs WHERE status='running'")
    for r in rows:
        sql("UPDATE a2_steps SET status='failed', error='interrupted by process restart' WHERE run_id=? AND status='running'", (r["id"],))
        set_status(r["id"], "failed", "interrupted by process restart; press Resume")
        event(r["id"], "warn", "Process restarted while this run was executing. Completed steps are kept; resume to continue.")
    return len(rows)


# ------------------------------------------------------------------ fact correction


def correct_fact(run_id: str, session_id: str, fact_id: str, patch: dict) -> int | None:
    """Apply a human correction -> new context version -> mark downstream steps superseded.
    Intake is NOT re-run (its input, the transcript, did not change); planner/review re-run because their input hash changes."""
    run = get_run(run_id, session_id)
    if not run:
        return None
    clean = {k: v for k, v in patch.items() if k in ("text", "owner", "deadline_text") and v is not None}
    if not clean:
        raise ValueError("patch must set at least one of text, owner, deadline_text")
    intake = q1("SELECT output_json FROM a2_steps WHERE run_id=? AND agent='intake' AND status='done' ORDER BY id DESC", (run_id,))
    if not intake:
        raise ValueError("run has no completed intake yet")
    n_facts = len(IntakeOutput.model_validate(jload(intake["output_json"])).facts)
    if not (fact_id.startswith("F") and fact_id[1:].isdigit() and 1 <= int(fact_id[1:]) <= n_facts):
        raise ValueError(f"unknown fact id {fact_id}")
    with tx() as c:
        v = c.execute("SELECT context_version FROM a2_runs WHERE id=?", (run_id,)).fetchone()["context_version"] + 1
        c.execute("UPDATE a2_runs SET context_version=?, status='pending', final_json=NULL, error=NULL, updated_at=? WHERE id=?", (v, now_iso(), run_id))
        c.execute("INSERT INTO a2_fact_overrides(run_id,context_version,fact_id,patch_json,created_at) VALUES(?,?,?,?,?)",
                  (run_id, v, fact_id, jdump(clean), now_iso()))
        c.execute("UPDATE a2_steps SET status='superseded' WHERE run_id=? AND agent IN ('planner','review') AND status='done' AND context_version<?", (run_id, v))
    event(run_id, "rerun", f"Fact {fact_id} corrected by user ({', '.join(clean)}). Context v{v}. Planner and Review will re-run; Intake output is unchanged and will be reused.")
    return v


def _overrides(run_id: str) -> list[dict]:
    return [{"fact_id": r["fact_id"], "patch": jload(r["patch_json"]), "context_version": r["context_version"]}
            for r in q("SELECT * FROM a2_fact_overrides WHERE run_id=? ORDER BY id", (run_id,))]


# ------------------------------------------------------------------ steps


def _canon(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)


def _hash(agent: str, rnd: int, inp: BaseModel) -> str:
    """Step identity. The round is included so each bounded revision is a genuinely new attempt, while a resume
    of the same round (same input) still matches and reuses the stored output."""
    s = get_settings()
    return hashlib.sha256(f"{agents.PROMPT_VERSION}|{s.model}|{agent}|{rnd}|{_canon(inp)}".encode()).hexdigest()[:24]


def _consume_fault(run_id: str, key_flag: str) -> bool:
    with tx() as c:
        row = c.execute("SELECT fault_json FROM a2_runs WHERE id=?", (run_id,)).fetchone()
        f = jload(row["fault_json"], {})
        if f.get(key_flag) and not f.get(key_flag + "_consumed"):
            f[key_flag + "_consumed"] = True
            c.execute("UPDATE a2_runs SET fault_json=? WHERE id=?", (jdump(f), run_id))
            return True
    return False


def _fault_cfg(run_id: str) -> dict:
    return jload(q1("SELECT fault_json FROM a2_runs WHERE id=?", (run_id,))["fault_json"], {})


def _check_stale(run_id: str, version: int) -> None:
    cur = q1("SELECT context_version FROM a2_runs WHERE id=?", (run_id,))["context_version"]
    if cur != version:
        raise Superseded()


def _step(run_id: str, agent: str, rnd: int, version: int, inp: BaseModel, out_cls: type[T], fn: Callable[[], T], exec_version: int | None = None) -> T:
    """`version` is recorded on the step row; `exec_version` is the context version this execution runs under
    (they differ for Intake, whose input is the transcript only)."""
    _check_stale(run_id, exec_version or version)
    h = _hash(agent, rnd, inp)
    prior = q1("SELECT * FROM a2_steps WHERE run_id=? AND agent=? AND input_hash=? AND status IN ('done','failed','running') ORDER BY id DESC LIMIT 1", (run_id, agent, h))
    if prior and prior["status"] == "done":
        event(run_id, "reuse", f"{agent} (round {rnd}) reused: identical input already processed, no model call made.")
        return out_cls.model_validate(jload(prior["output_json"]))
    if prior:  # failed or orphaned 'running' -> retry the same step row, count the attempt
        step_id = prior["id"]
        sql("UPDATE a2_steps SET status='running', error=NULL, attempts=attempts+1, started_at=?, finished_at=NULL WHERE id=?", (now_iso(), step_id))
        event(run_id, "rerun", f"{agent} (round {rnd}): retrying failed step, attempt {prior['attempts'] + 1}.")
    else:
        step_id = sql(
            "INSERT INTO a2_steps(run_id,agent,round,context_version,input_hash,status,input_json,started_at) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, agent, rnd, version, h, "running", _canon(inp), now_iso()),
        )
    try:
        fcfg = _fault_cfg(run_id)
        if fcfg.get("fail_agent") == agent and _consume_fault(run_id, "fail_agent"):
            raise SimulatedModelFailure(f"[SIMULATED FAULT] model outage injected for the {agent} agent")
        out = fn()
    except SimulatedModelFailure as e:
        sql("UPDATE a2_steps SET status='failed', error=?, simulated_fault=1, finished_at=? WHERE id=?", (str(e), now_iso(), step_id))
        event(run_id, "fault", str(e))
        raise StepFailed(agent, str(e), True) from e
    except LLMError as e:
        sql("UPDATE a2_steps SET status='failed', error=?, finished_at=? WHERE id=?", (str(e)[:500], now_iso(), step_id))
        event(run_id, "error", f"{agent} failed: {e}")
        raise StepFailed(agent, str(e), False) from e
    sql("UPDATE a2_steps SET status='done', output_json=?, finished_at=? WHERE id=?", (_canon(out), now_iso(), step_id))
    return out


def _handoff(run_id: str, frm: str, to: str, kind: str, version: int, build: Callable[[], BaseModel | dict]) -> BaseModel | dict:
    """Validate by building the receiver's input model; persist the message either way."""
    seq = (q1("SELECT COALESCE(MAX(seq),0)+1 AS n FROM a2_handoffs WHERE run_id=?", (run_id,))["n"])
    try:
        payload = build()
    except ValidationError as e:
        sql("INSERT INTO a2_handoffs(run_id,seq,from_agent,to_agent,kind,context_version,valid,validation_error,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, seq, frm, to, kind, version, 0, e.json(include_url=False)[:800], "{}", now_iso()))
        raise HandoffError(f"invalid {frm}->{to} handoff: {e}") from e
    body = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    sql("INSERT INTO a2_handoffs(run_id,seq,from_agent,to_agent,kind,context_version,valid,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (run_id, seq, frm, to, kind, version, 1, jdump(body), now_iso()))
    return payload


# ------------------------------------------------------------------ fault: injected rule violation


def _inject_weekend_deadline(plan: PlanOutput, meeting_date: str) -> PlanOutput:
    d = date.fromisoformat(meeting_date)
    saturday = d + timedelta(days=(5 - d.weekday()) % 7 or 7)
    plan = plan.model_copy(deep=True)
    target = next((t for t in plan.tasks if t.deadline), plan.tasks[0] if plan.tasks else None)
    if target:
        target.deadline = saturday.isoformat()
    return plan


# ------------------------------------------------------------------ pipeline


def execute_run(run_id: str) -> None:
    """Run (or resume) the pipeline. Serialised per run; safe to call repeatedly."""
    with _lock(run_id):
        row = q1("SELECT * FROM a2_runs WHERE id=?", (run_id,))
        if not row:
            return
        version = row["context_version"]
        set_status(run_id, "running")
        scope = f"a2:run:{run_id}"
        settings = get_settings()
        try:
            rules = RulesDoc.model_validate(jload(row["rules_json"]))

            # ---- 1. Intake (input = transcript only; unaffected by fact corrections)
            iin = IntakeInput(transcript=row["transcript"], meeting_date=row["meeting_date"])
            intake_out = _step(run_id, "intake", 0, 1, iin, IntakeOutput, lambda: agents.run_intake(scope, iin), exec_version=version)

            def ctx() -> Context:
                return build_context(intake_out, row["transcript"], row["meeting_date"], version, _overrides(run_id))

            # ---- 2. Intake -> Planner handoff
            pin: PlannerInput = _handoff(run_id, "intake", "planner", "handoff", version,
                                         lambda: PlannerInput(context=ctx(), rules=rules))  # type: ignore[assignment]
            plan = _step(run_id, "planner", 0, version, pin, PlanOutput,
                         lambda: _maybe_inject(run_id, agents.run_planner(scope, pin), row["meeting_date"]))

            # ---- 3. bounded Planner <-> Review loop
            issues: list[Issue] = []
            approved = False
            for rnd in range(settings.a2_max_review_rounds):
                static = static_check(plan, pin.context, rules)
                rin: ReviewInput = _handoff(run_id, "planner", "review", "handoff", version,
                                            lambda: ReviewInput(transcript=row["transcript"], context=pin.context, rules=rules, plan=plan, static_findings=static))  # type: ignore[assignment]
                review = _step(run_id, "review", rnd, version, rin, ReviewOutput, lambda: agents.run_review(scope, rin))
                issues = dedupe_issues(static + review.issues)
                blocking = [i for i in issues if i.severity in ("blocker", "major")]
                if review.verdict == "approved" and not blocking:
                    approved = True
                    break
                if review.verdict == "approved" and blocking:
                    event(run_id, "warn", f"Review round {rnd + 1}: reviewer said 'approved' but deterministic validators found {len(blocking)} blocking issue(s); overriding to changes_required.")
                if rnd == settings.a2_max_review_rounds - 1:
                    _handoff(run_id, "review", "human", "escalation", version, lambda: {"unresolved_issues": [i.model_dump(mode="json") for i in issues], "rounds": rnd + 1})
                    event(run_id, "warn", f"Review limit reached after {rnd + 1} round(s); {len(blocking)} issue(s) unresolved. Escalated to human.")
                    break
                fb: PlannerInput = _handoff(run_id, "review", "planner", "feedback", version,
                                            lambda: PlannerInput(context=pin.context, rules=rules, feedback=issues, previous_plan=plan))  # type: ignore[assignment]
                event(run_id, "info", f"Review round {rnd + 1}: {len(blocking)} blocking issue(s) sent back to Planner.")
                plan = _step(run_id, "planner", rnd + 1, version, fb, PlanOutput, lambda: agents.run_planner(scope, fb))
                pin = fb

            final = {
                "status": "approved" if approved else "needs_human",
                "context_version": version,
                "plan": plan.model_dump(mode="json"),
                "unresolved_issues": [] if approved else [i.model_dump(mode="json") for i in issues],
                "rounds": rnd + 1,
            }
            _check_stale(run_id, version)
            set_status(run_id, "done" if approved else "needs_human", final=final)
            event(run_id, "info", "Run finished: " + ("plan approved by Review Agent." if approved else "needs human attention."))
        except Superseded:
            event(run_id, "info", f"Execution for context v{version} abandoned: a newer context version exists.")
        except StepFailed as e:
            set_status(run_id, "failed", f"{e.agent}: {e}")
        except HandoffError as e:
            event(run_id, "error", str(e))
            set_status(run_id, "failed", str(e))
        except Exception as e:  # never leave a run stuck in 'running'
            event(run_id, "error", f"unexpected {type(e).__name__}: {e}")
            set_status(run_id, "failed", f"unexpected {type(e).__name__}: {e}")


def _maybe_inject(run_id: str, plan: PlanOutput, meeting_date: str) -> PlanOutput:
    if _fault_cfg(run_id).get("inject_violation") and _consume_fault(run_id, "inject_violation"):
        event(run_id, "fault", "[SIMULATED FAULT] the fault injector changed one task deadline in the Planner's first output to a Saturday. The model did not produce this violation; the Review step should catch and return it.")
        return _inject_weekend_deadline(plan, meeting_date)
    return plan


def start_async(run_id: str) -> None:
    threading.Thread(target=execute_run, args=(run_id,), daemon=True, name=f"a2-{run_id}").start()


# ------------------------------------------------------------------ read model for the UI


def run_view(run_id: str, session_id: str) -> dict | None:
    run = get_run(run_id, session_id)
    if not run:
        return None
    steps = q("SELECT * FROM a2_steps WHERE run_id=? ORDER BY id", (run_id,))
    handoffs = q("SELECT * FROM a2_handoffs WHERE run_id=? ORDER BY seq", (run_id,))
    events = q("SELECT * FROM a2_events WHERE run_id=? ORDER BY id", (run_id,))
    cost = q1("SELECT COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, COUNT(*) n FROM llm_calls WHERE scope=?", (f"a2:run:{run_id}",))
    intake = next((s for s in reversed(steps) if s["agent"] == "intake" and s["status"] == "done"), None)
    context = None
    if intake:
        context = build_context(IntakeOutput.model_validate(jload(intake["output_json"])), run["transcript"], run["meeting_date"],
                                run["context_version"], _overrides(run_id)).model_dump(mode="json")
    agent_state: dict[str, dict] = {}
    for a in ("intake", "planner", "review"):
        mine = [s for s in steps if s["agent"] == a]
        latest = mine[-1] if mine else None
        agent_state[a] = {"status": latest["status"] if latest else "idle", "runs": len(mine)}
    return {
        "id": run["id"], "status": run["status"], "error": run["error"], "context_version": run["context_version"],
        "meeting_date": run["meeting_date"], "transcript": run["transcript"], "fault": jload(run["fault_json"], {}),
        "rules": jload(run["rules_json"]), "context": context, "final": jload(run["final_json"]),
        "agents": agent_state,
        "steps": [{
            "id": s["id"], "agent": s["agent"], "round": s["round"], "context_version": s["context_version"], "status": s["status"],
            "attempts": s["attempts"], "simulated_fault": bool(s["simulated_fault"]), "error": s["error"],
            "input": jload(s["input_json"]), "output": jload(s["output_json"]), "started_at": s["started_at"], "finished_at": s["finished_at"],
        } for s in steps],
        "handoffs": [{
            "seq": h["seq"], "from": h["from_agent"], "to": h["to_agent"], "kind": h["kind"], "context_version": h["context_version"],
            "valid": bool(h["valid"]), "validation_error": h["validation_error"], "payload": jload(h["payload_json"]),
        } for h in handoffs],
        "events": [{"kind": e["kind"], "text": e["text"], "at": e["created_at"]} for e in events],
        "usage": {"calls": cost["n"], "input_tokens": cost["i"], "output_tokens": cost["o"], "cost_usd": round(cost["c"], 4)},
    }
