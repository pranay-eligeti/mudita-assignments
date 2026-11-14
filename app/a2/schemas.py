"""Typed contracts for the three agents and every handoff between them.

Each agent has its own input and output model. The orchestrator builds the *input* model of the receiving
agent from the sender's validated output, so a malformed handoff fails before any model call is made.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------- rules (company rules document)


class RuleCheck(BaseModel):
    """Optional machine-checkable form of a rule. Rules without a check are judged by the Review Agent only."""

    type: Literal["business_day", "max_tasks_per_owner", "dependency"]
    max: Optional[int] = None
    if_title_matches: Optional[str] = None          # regex, case-insensitive
    must_depend_on_matches: Optional[str] = None    # regex, case-insensitive


class Rule(BaseModel):
    id: str
    text: str
    check: Optional[RuleCheck] = None


class RulesDoc(BaseModel):
    rules: list[Rule]
    roles: list[str] = Field(default_factory=list, description="Functional roles that may own tasks even if not named in the transcript")


# ---------------------------------------------------------------- intake


class SourceRef(BaseModel):
    line_start: int
    line_end: int
    quote: str = Field(description="Exact words copied from the transcript lines")


class RawFact(BaseModel):
    kind: Literal["decision", "requirement", "constraint"]
    text: str = Field(description="One atomic statement, paraphrased faithfully")
    source: SourceRef
    owner_stated: Optional[str] = Field(None, description="Person/role the transcript explicitly assigns to this. null if none stated. NEVER guess.")
    deadline_stated: Optional[str] = Field(None, description="Deadline wording exactly as spoken (e.g. 'by Friday'). null if none stated. NEVER guess.")


class RawGap(BaseModel):
    kind: Literal["missing", "conflict"]
    description: str
    sources: list[SourceRef] = Field(default_factory=list)


class IntakeOutput(BaseModel):
    facts: list[RawFact]
    gaps: list[RawGap] = Field(default_factory=list)
    participants: list[str] = Field(default_factory=list, description="People who speak or are named in the transcript")


class IntakeInput(BaseModel):
    transcript: str
    meeting_date: str  # YYYY-MM-DD


class Fact(BaseModel):
    """A fact in the shared context: intake output + stable id + verification + user corrections."""

    id: str
    kind: Literal["decision", "requirement", "constraint"]
    text: str
    source: SourceRef
    owner_stated: Optional[str] = None
    deadline_stated: Optional[str] = None
    quote_verified: bool = True
    corrected: bool = False
    original_text: Optional[str] = None


class Gap(BaseModel):
    id: str
    kind: Literal["missing", "conflict", "ungrounded"]
    description: str
    sources: list[SourceRef] = Field(default_factory=list)


class Context(BaseModel):
    """Versioned shared source-of-truth handed to Planner and Review."""

    version: int
    meeting_date: str
    participants: list[str]
    facts: list[Fact]
    gaps: list[Gap]


# ---------------------------------------------------------------- planning


class Task(BaseModel):
    id: str = Field(description="T1, T2, ...")
    title: str
    owner: Optional[str] = Field(None, description="Must be a person named in the transcript or a role from the rules; null if unknown")
    deadline: Optional[str] = Field(None, description="YYYY-MM-DD. Only if derivable from a stated deadline or a rule; else null")
    depends_on: list[str] = Field(default_factory=list)
    basis: Literal["supported", "recommendation"] = Field(description="supported = directly grounded in cited facts; recommendation = planner's own suggestion")
    fact_refs: list[str] = Field(default_factory=list)
    rule_refs: list[str] = Field(default_factory=list)
    rationale: str = ""

    @field_validator("owner")
    @classmethod
    def _blank_owner(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return None if v.lower() in {"", "unassigned", "tbd", "none", "unknown"} else v


class PlanOutput(BaseModel):
    tasks: list[Task]
    supported_facts: list[str] = Field(default_factory=list, description="Fact ids the plan relies on")
    recommendations: list[str] = Field(default_factory=list, description="Planner suggestions NOT stated in the transcript")
    unresolved_questions: list[str] = Field(default_factory=list)
    revision_notes: list[str] = Field(default_factory=list, description="When revising: one line per reviewer issue explaining the fix")


# ---------------------------------------------------------------- review


class Issue(BaseModel):
    id: str = ""
    severity: Literal["blocker", "major", "minor"]
    source: Literal["llm", "static"] = "llm"
    task_id: Optional[str] = None
    rule_id: Optional[str] = None
    fact_ref: Optional[str] = None
    problem: str
    required_correction: str


class ReviewOutput(BaseModel):
    verdict: Literal["approved", "changes_required"]
    issues: list[Issue] = Field(default_factory=list)
    checked_rules: list[str] = Field(default_factory=list, description="Rule ids that were checked")


class PlannerInput(BaseModel):
    context: Context
    rules: RulesDoc
    feedback: list[Issue] = Field(default_factory=list)
    previous_plan: Optional[PlanOutput] = None


class ReviewInput(BaseModel):
    transcript: str
    context: Context
    rules: RulesDoc
    plan: PlanOutput
    static_findings: list[Issue] = Field(default_factory=list)
