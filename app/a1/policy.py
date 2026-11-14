"""Editable business policy. Loaded from the DB override if present, else the bundled policy.json.
The LLM never sees permission to change this; qualification and routing are plain Python over this document."""
from __future__ import annotations

import json
from datetime import time
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from ..db import kv_del, kv_get, kv_set

DEFAULT_PATH = Path(__file__).with_name("policy.json")
KV_KEY = "a1_policy_json"


class WorkingHours(BaseModel):
    days: list[int] = Field(default=[0, 1, 2, 3, 4], description="0=Monday")
    start: str = "10:00"
    end: str = "17:00"

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        time.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _order(self) -> "WorkingHours":
        if time.fromisoformat(self.start) >= time.fromisoformat(self.end):
            raise ValueError("working_hours.start must be before end")
        return self


class Service(BaseModel):
    id: str
    name: str
    description: str = ""


class Qualification(BaseModel):
    required_fields: list[Literal["service", "company_name", "budget_usd", "start_within_weeks"]]
    min_budget_usd: float
    max_start_within_weeks: float


class Rep(BaseModel):
    id: str
    name: str
    email: str = ""
    services: list[str] = []
    calendar_id: str = "primary"


class RoutingWhen(BaseModel):
    min_budget_usd: Optional[float] = None
    service_in: Optional[list[str]] = None


class RoutingRule(BaseModel):
    id: str
    description: str = ""
    when: RoutingWhen
    assign: str


class Policy(BaseModel):
    company: str
    description: str = ""
    timezone: str
    working_hours: WorkingHours
    slot_minutes: int = Field(30, ge=10, le=240)
    lookahead_days: int = Field(7, ge=1, le=30)
    min_notice_hours: float = Field(2, ge=0)
    services: list[Service]
    qualification: Qualification
    reps: list[Rep] = Field(min_length=1)
    routing_rules: list[RoutingRule]
    fallback_routing: Literal["least_loaded"] = "least_loaded"

    @model_validator(mode="after")
    def _consistent(self) -> "Policy":
        ZoneInfo(self.timezone)  # raises if unknown
        sids = {s.id for s in self.services}
        rids = {r.id for r in self.reps}
        for r in self.reps:
            bad = set(r.services) - sids
            if bad:
                raise ValueError(f"rep {r.id} lists unknown services {sorted(bad)}")
        for rr in self.routing_rules:
            if rr.assign not in rids:
                raise ValueError(f"routing rule {rr.id} assigns unknown rep '{rr.assign}'")
            for s in rr.when.service_in or []:
                if s not in sids:
                    raise ValueError(f"routing rule {rr.id} references unknown service '{s}'")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def service(self, sid: str | None) -> Service | None:
        return next((s for s in self.services if s.id == sid), None)

    def rep(self, rid: str | None) -> Rep | None:
        return next((r for r in self.reps if r.id == rid), None)


def load_policy() -> Policy:
    raw = kv_get(KV_KEY)
    if raw:
        try:
            return Policy.model_validate_json(raw)
        except Exception:  # corrupted override must never take the app down
            kv_del(KV_KEY)
    return Policy.model_validate_json(DEFAULT_PATH.read_text())


def save_policy(raw_json: str) -> Policy:
    p = Policy.model_validate_json(raw_json)  # raises ValidationError / ValueError for invalid policy
    kv_set(KV_KEY, json.dumps(p.model_dump(mode="json"), indent=2))
    return p


def reset_policy() -> Policy:
    kv_del(KV_KEY)
    return load_policy()
