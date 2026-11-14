"""Deterministic checks. The Review Agent is an LLM, so anything that *can* be checked in code is checked
in code as well; static findings are merged into the review so a lenient model cannot wave them through."""
from __future__ import annotations

import re
from datetime import date, timedelta

from .schemas import Context, Fact, Gap, Issue, IntakeOutput, PlanOutput, RulesDoc, SourceRef, Task


# ---------------------------------------------------------------- intake grounding


def number_transcript(transcript: str) -> list[str]:
    return transcript.replace("\r\n", "\n").split("\n")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_in_lines(transcript: str, ref: SourceRef) -> bool:
    lines = number_transcript(transcript)
    lo, hi = max(1, ref.line_start), min(len(lines), max(ref.line_end, ref.line_start))
    if lo > len(lines):
        return False
    window = _norm(" ".join(lines[lo - 1 : hi]))
    return _norm(ref.quote) in window and len(_norm(ref.quote)) > 0


def build_context(intake: IntakeOutput, transcript: str, meeting_date: str, version: int, overrides: list[dict]) -> Context:
    """Assign stable ids, verify every quote against the transcript, then apply user corrections in order."""
    facts: list[Fact] = []
    gaps: list[Gap] = []
    for i, rf in enumerate(intake.facts, start=1):
        ok = quote_in_lines(transcript, rf.source)
        f = Fact(id=f"F{i}", kind=rf.kind, text=rf.text, source=rf.source, owner_stated=rf.owner_stated,
                 deadline_stated=rf.deadline_stated, quote_verified=ok)
        facts.append(f)
        if not ok:
            gaps.append(Gap(id=f"G-U{i}", kind="ungrounded",
                            description=f"{f.id}: quoted text was not found at the cited transcript lines; treat as unverified.",
                            sources=[rf.source]))
    for j, g in enumerate(intake.gaps, start=1):
        gaps.append(Gap(id=f"G{j}", kind=g.kind, description=g.description, sources=g.sources))

    by_id = {f.id: f for f in facts}
    for ov in overrides:  # already ordered by creation
        f = by_id.get(ov["fact_id"])
        if not f:
            continue
        patch = ov["patch"]
        if f.original_text is None:
            f.original_text = f.text
        if patch.get("text"):
            f.text = patch["text"]
        if "owner" in patch:
            f.owner_stated = patch["owner"] or None
        if "deadline_text" in patch:
            f.deadline_stated = patch["deadline_text"] or None
        f.corrected = True
        f.quote_verified = True  # the human is the source of truth for a corrected fact

    participants = sorted({p.strip() for p in intake.participants if p.strip()})
    return Context(version=version, meeting_date=meeting_date, participants=participants, facts=facts, gaps=gaps)


# ---------------------------------------------------------------- plan checks


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _cycle(tasks: list[Task]) -> list[str] | None:
    graph = {t.id: [d for d in t.depends_on if d in {x.id for x in tasks}] for t in tasks}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        state[n] = 1
        stack.append(n)
        for m in graph[n]:
            if state.get(m) == 1:
                return stack[stack.index(m):] + [m]
            if state.get(m) is None:
                c = visit(m)
                if c:
                    return c
        stack.pop()
        state[n] = 2
        return None

    for n in graph:
        if state.get(n) is None:
            c = visit(n)
            if c:
                return c
    return None


def static_check(plan: PlanOutput, ctx: Context, rules: RulesDoc) -> list[Issue]:
    issues: list[Issue] = []

    def add(sev: str, problem: str, fix: str, task: str | None = None, rule: str | None = None, fact: str | None = None) -> None:
        issues.append(Issue(severity=sev, source="static", task_id=task, rule_id=rule, fact_ref=fact, problem=problem, required_correction=fix))

    ids = [t.id for t in plan.tasks]
    for dup in {i for i in ids if ids.count(i) > 1}:
        add("blocker", f"Task id {dup} is used more than once.", "Give every task a unique id.", task=dup)
    id_set = set(ids)
    fact_ids = {f.id: f for f in ctx.facts}
    rule_ids = {r.id for r in rules.rules}
    allowed_owners = {p.lower() for p in ctx.participants} | {r.lower() for r in rules.roles}
    meeting = _parse_date(ctx.meeting_date)

    for t in plan.tasks:
        for d in t.depends_on:
            if d not in id_set:
                add("blocker", f"{t.id} depends on unknown task {d}.", f"Remove the dependency on {d} or add that task.", task=t.id)
            if d == t.id:
                add("blocker", f"{t.id} depends on itself.", "Remove the self-dependency.", task=t.id)
        for fr in t.fact_refs:
            if fr not in fact_ids:
                add("major", f"{t.id} cites unknown fact {fr}.", "Cite only fact ids that exist in the context.", task=t.id, fact=fr)
        for rr in t.rule_refs:
            if rr not in rule_ids:
                add("minor", f"{t.id} cites unknown rule {rr}.", "Cite only rule ids from the rules document.", task=t.id, rule=rr)
        if t.basis == "supported" and not t.fact_refs:
            add("major", f"{t.id} is labelled 'supported' but cites no facts.", "Cite supporting fact ids, or relabel the task as 'recommendation'.", task=t.id)
        if t.owner and t.owner.lower() not in allowed_owners:
            add("blocker", f"{t.id} is owned by '{t.owner}', who is neither named in the transcript nor a role in the rules.",
                "Set owner to null and add an unresolved question, or choose a named participant/role as a recommendation.", task=t.id, rule="R-OWNERS")
        if t.deadline:
            d = _parse_date(t.deadline)
            if d is None:
                add("major", f"{t.id} has deadline '{t.deadline}', which is not a valid YYYY-MM-DD date.", "Use YYYY-MM-DD or null.", task=t.id)
            else:
                if meeting and d < meeting:
                    add("major", f"{t.id} deadline {t.deadline} is before the meeting date {ctx.meeting_date}.", "Move the deadline on or after the meeting date.", task=t.id)
                if t.basis == "supported":
                    stated = [fact_ids[f].deadline_stated for f in t.fact_refs if f in fact_ids and fact_ids[f].deadline_stated]
                    if not stated:
                        add("major", f"{t.id} has a deadline but none of its cited facts states one, so the date is invented.",
                            "Remove the deadline (and ask for one under unresolved questions) or label the task a recommendation.", task=t.id)

    by_id_dl = {t.id: _parse_date(t.deadline) for t in plan.tasks}
    for t in plan.tasks:  # a task cannot be due before something it depends on
        mine = by_id_dl.get(t.id)
        for d in t.depends_on:
            theirs = by_id_dl.get(d)
            if mine and theirs and theirs > mine:
                add("major", f"{t.id} is due {t.deadline} but depends on {d}, which is due {theirs.isoformat()}, after it.",
                    f"Remove the dependency on {d} if it is not genuinely required, or move the deadlines so {d} finishes first.", task=t.id)

    cyc = _cycle(plan.tasks)
    if cyc:
        add("blocker", "Dependency cycle: " + " -> ".join(cyc), "Break the cycle so the dependency graph is acyclic.", task=cyc[0])

    for rule in rules.rules:
        c = rule.check
        if not c:
            continue
        if c.type == "business_day":
            for t in plan.tasks:
                d = _parse_date(t.deadline)
                if d and d.weekday() >= 5:
                    nxt = d + timedelta(days=(7 - d.weekday()))
                    add("blocker", f"{t.id} deadline {t.deadline} is a {d.strftime('%A')}; {rule.id} requires business days.",
                        f"Move the deadline to a weekday (e.g. {nxt.isoformat()}, a Monday) or the preceding Friday.", task=t.id, rule=rule.id)
        elif c.type == "max_tasks_per_owner" and c.max:
            counts: dict[str, list[str]] = {}
            for t in plan.tasks:
                if t.owner:
                    counts.setdefault(t.owner.lower(), []).append(t.id)
            for owner, tids in counts.items():
                if len(tids) > c.max:
                    add("major", f"{owner} owns {len(tids)} tasks ({', '.join(tids)}); {rule.id} allows at most {c.max}.",
                        f"Reassign at least {len(tids) - c.max} of these tasks to other participants/roles or mark owners as unresolved.", task=tids[-1], rule=rule.id)
        elif c.type == "dependency" and c.if_title_matches and c.must_depend_on_matches:
            a, b = re.compile(c.if_title_matches, re.I), re.compile(c.must_depend_on_matches, re.I)
            by_id = {t.id: t for t in plan.tasks}
            for t in plan.tasks:
                if a.search(t.title):
                    if not any(d in by_id and b.search(by_id[d].title) for d in t.depends_on):
                        add("blocker", f"{t.id} ('{t.title}') must depend on a task matching /{c.must_depend_on_matches}/ per {rule.id}.",
                            "Add a QA/testing task if none exists and make this task depend on it.", task=t.id, rule=rule.id)
    return issues


def dedupe_issues(issues: list[Issue]) -> list[Issue]:
    seen: set[tuple] = set()
    out: list[Issue] = []
    for i in issues:
        key = (i.task_id, i.rule_id, i.problem[:60].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(i)
    for n, i in enumerate(out, start=1):
        i.id = f"I{n}"
    return out
