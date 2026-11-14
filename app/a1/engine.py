"""Deterministic qualification, routing and slot generation. No LLM, no I/O: pure functions over policy + fields.

This is the policy-enforcement boundary. The model may *extract* facts a person stated; it can never decide
classification, assignment or availability, so an instruction like "ignore your rules" has nothing to act on."""
from __future__ import annotations

import hashlib
import base64
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

from .policy import Policy

FIELD_LABELS = {
    "service": "which service you need",
    "company_name": "your company name",
    "budget_usd": "your approximate budget in USD",
    "start_within_weeks": "how many weeks from now you would like to start",
}


@dataclass
class Classification:
    label: str                 # qualified | not_qualified | needs_more_info
    trace: list[dict[str, Any]]
    missing: list[str]
    reasons: list[str]         # human-readable reasons for not_qualified


def classify(fields: dict[str, Any], policy: Policy) -> Classification:
    """Precedence: hard disqualifiers (known values that violate policy) > missing required fields > qualified."""
    q = policy.qualification
    trace: list[dict[str, Any]] = []
    reasons: list[str] = []

    svc = fields.get("service")
    if svc is not None:
        if policy.service(svc):
            trace.append({"rule": "Q-SERVICE", "result": "pass", "detail": f"'{svc}' is an offered service"})
        else:
            trace.append({"rule": "Q-SERVICE", "result": "fail", "detail": f"'{svc}' is not an offered service"})
            reasons.append("that isn't a service we offer (" + ", ".join(s.name for s in policy.services) + ")")
    else:
        trace.append({"rule": "Q-SERVICE", "result": "missing", "detail": "service not stated"})

    budget = fields.get("budget_usd")
    if budget is not None:
        if budget >= q.min_budget_usd:
            trace.append({"rule": "Q-BUDGET", "result": "pass", "detail": f"{budget:g} >= minimum {q.min_budget_usd:g} USD"})
        else:
            trace.append({"rule": "Q-BUDGET", "result": "fail", "detail": f"{budget:g} < minimum {q.min_budget_usd:g} USD"})
            reasons.append(f"our minimum engagement is {q.min_budget_usd:,.0f} USD")
    else:
        trace.append({"rule": "Q-BUDGET", "result": "missing", "detail": "budget not stated"})

    start = fields.get("start_within_weeks")
    if start is not None:
        if start <= q.max_start_within_weeks:
            trace.append({"rule": "Q-START", "result": "pass", "detail": f"{start:g} weeks <= {q.max_start_within_weeks:g}"})
        else:
            trace.append({"rule": "Q-START", "result": "fail", "detail": f"{start:g} weeks > {q.max_start_within_weeks:g}"})
            reasons.append(f"we only take projects that can start within {q.max_start_within_weeks:g} weeks")
    else:
        trace.append({"rule": "Q-START", "result": "missing", "detail": "start timeline not stated"})

    if fields.get("company_name"):
        trace.append({"rule": "Q-COMPANY", "result": "pass", "detail": "company name stated"})
    else:
        trace.append({"rule": "Q-COMPANY", "result": "missing", "detail": "company name not stated"})

    missing = [f for f in q.required_fields if fields.get(f) in (None, "")]
    if reasons:
        return Classification("not_qualified", trace, missing, reasons)
    if missing:
        return Classification("needs_more_info", trace, missing, [])
    return Classification("qualified", trace, [], [])


def route(fields: dict[str, Any], policy: Policy, open_counts: dict[str, int]) -> tuple[str, list[dict[str, Any]]]:
    """First matching explicit rule wins. Falls back to least-loaded rep (ties -> policy order)."""
    trace: list[dict[str, Any]] = []
    budget, svc = fields.get("budget_usd"), fields.get("service")
    for rule in policy.routing_rules:
        w = rule.when
        checks = []
        if w.min_budget_usd is not None:
            checks.append(budget is not None and budget >= w.min_budget_usd)
        if w.service_in is not None:
            checks.append(svc in w.service_in)
        hit = bool(checks) and all(checks)
        trace.append({"rule": rule.id, "result": "match" if hit else "no match", "detail": rule.description})
        if hit:
            return rule.assign, trace
    rep = min(policy.reps, key=lambda r: (open_counts.get(r.id, 0), policy.reps.index(r)))
    trace.append({"rule": "FALLBACK", "result": "match", "detail": f"least loaded rep: {rep.name} ({open_counts.get(rep.id, 0)} open)"})
    return rep.id, trace


# ---------------------------------------------------------------- slots


def candidate_slots(policy: Policy, now: datetime | None = None) -> list[tuple[datetime, datetime]]:
    """All policy-valid slot windows (UTC-aware) in the lookahead, ignoring calendar state."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tz = policy.tz
    wh = policy.working_hours
    start_t, end_t = time.fromisoformat(wh.start), time.fromisoformat(wh.end)
    earliest = now + timedelta(hours=policy.min_notice_hours)
    step = timedelta(minutes=policy.slot_minutes)
    out: list[tuple[datetime, datetime]] = []
    local_today = now.astimezone(tz).date()
    for d in range(policy.lookahead_days + 1):
        day = local_today + timedelta(days=d)
        if day.weekday() not in wh.days:
            continue
        cur = datetime.combine(day, start_t, tzinfo=tz)
        end_of_day = datetime.combine(day, end_t, tzinfo=tz)
        while cur + step <= end_of_day:
            s = cur.astimezone(timezone.utc)
            if s >= earliest:
                out.append((s, (cur + step).astimezone(timezone.utc)))
            cur += step
    return out


def overlaps(a: tuple[datetime, datetime], busy: list[tuple[datetime, datetime]]) -> bool:
    return any(a[0] < b_end and b_start < a[1] for b_start, b_end in busy)


def pick_spread(slots: list[tuple[datetime, datetime]], policy: Policy, n: int = 3) -> list[tuple[datetime, datetime]]:
    """Prefer one slot on each of the next days; fill from remaining if fewer days exist."""
    chosen: list[tuple[datetime, datetime]] = []
    days: set = set()
    for s in slots:
        d = s[0].astimezone(policy.tz).date()
        if d not in days:
            chosen.append(s)
            days.add(d)
        if len(chosen) == n:
            return chosen
    for s in slots:
        if s not in chosen:
            chosen.append(s)
        if len(chosen) == n:
            break
    return sorted(chosen)


def fmt_slot(start_iso: str, end_iso: str, policy: Policy) -> str:
    s = datetime.fromisoformat(start_iso).astimezone(policy.tz)
    e = datetime.fromisoformat(end_iso).astimezone(policy.tz)
    return f"{s.strftime('%a %d %b, %H:%M')}–{e.strftime('%H:%M')} ({policy.timezone})"


def event_id_for(lead_id: int, rep_id: str, start_iso: str) -> str:
    """Deterministic Google Calendar event id (base32hex, lowercase, 5-1024 chars). Same lead+rep+slot -> same id,
    so a repeated confirmation can never create a second event: Calendar answers 409 for a duplicate id."""
    start_utc = datetime.fromisoformat(start_iso).astimezone(timezone.utc).strftime("%Y%m%dT%H%M")
    digest = hashlib.sha1(f"lead{lead_id}|{rep_id}|{start_utc}".encode()).digest()
    return "lm" + base64.b32hexencode(digest).decode().lower().rstrip("=")
