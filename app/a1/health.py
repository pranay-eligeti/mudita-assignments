"""Visible integration state (channel_health) and the per-lead action log."""
from __future__ import annotations

from ..db import now_iso, q, q1, run


def set_health(channel: str, status: str, detail: str | None = None, *, inbound: bool = False, outbound: bool = False) -> None:
    ts = now_iso()
    run(
        "INSERT INTO channel_health(channel,status,detail,last_inbound_at,last_outbound_at,updated_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(channel) DO UPDATE SET status=excluded.status, detail=excluded.detail, "
        "last_inbound_at=COALESCE(?, channel_health.last_inbound_at), last_outbound_at=COALESCE(?, channel_health.last_outbound_at), updated_at=excluded.updated_at",
        (channel, status, detail, ts if inbound else None, ts if outbound else None, ts, ts if inbound else None, ts if outbound else None),
    )


def get_health() -> dict[str, dict]:
    return {r["channel"]: dict(r) for r in q("SELECT * FROM channel_health")}


def log(lead_id: int | None, kind: str, detail: str, level: str = "info") -> None:
    run("INSERT INTO actions(lead_id,level,kind,detail,created_at) VALUES(?,?,?,?,?)", (lead_id, level, kind, detail[:1000], now_iso()))
