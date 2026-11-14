"""Availability and booking against Google Calendar.

Why OAuth + a deterministic event id (design justification)
* One consented test account (OAuth, offline refresh token) works with a plain Gmail/Google account and needs no
  Workspace admin; a service account would also work but requires calendar sharing and hides the 'expired connection' case.
* Event ids are derived from (lead, rep, slot). Google rejects a duplicate id with HTTP 409, so a repeated confirmation,
  a webhook retry or two racing threads cannot produce two events - the database UNIQUE(event_id) is a second guard.
* Before inserting we re-read free/busy. We never blindly retry an uncertain write: we first GET the event by id to learn
  what really happened, and if even that fails the booking stays 'uncertain' and is shown to a human.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import get_conn, now_iso, q, q1, run
from . import google
from .engine import candidate_slots, event_id_for, overlaps, pick_spread
from .health import log, set_health
from .policy import Policy, Rep

ACTIVE = ("booking", "confirmed", "uncertain")


@dataclass
class BookResult:
    status: str   # confirmed | already_confirmed | slot_taken | uncertain | failed | in_progress | auth_expired | lead_already_booked
    message: str
    booking: dict[str, Any] | None = None


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def _internal_busy(rep_id: str, exclude_event_id: str | None = None) -> list[tuple[datetime, datetime]]:
    rows = q("SELECT start_iso,end_iso,event_id FROM bookings WHERE rep_id=? AND status IN ('booking','confirmed','uncertain')", (rep_id,))
    return [(datetime.fromisoformat(r["start_iso"]), datetime.fromisoformat(r["end_iso"])) for r in rows if r["event_id"] != exclude_event_id]


def available_slots(policy: Policy, rep: Rep, n: int = 3, now: datetime | None = None) -> list[tuple[str, str]]:
    """Valid policy slots that are free on the rep's calendar and not held by another in-flight booking.
    Raises google.AuthExpired / google.ApiError so the caller can surface the failure."""
    cands = candidate_slots(policy, now)
    if not cands:
        return []
    t_min, t_max = cands[0][0], cands[-1][1]
    try:
        busy = google.freebusy([rep.calendar_id], t_min, t_max)[rep.calendar_id]
        set_health("calendar", "ok", "free/busy read ok")
    except google.AuthExpired as e:
        set_health("calendar", "expired", str(e))
        raise
    except google.ApiError as e:
        set_health("calendar", "error", str(e))
        raise
    busy = busy + _internal_busy(rep.id)
    free = [c for c in cands if not overlaps(c, busy)]
    return [(_iso(s), _iso(e)) for s, e in pick_spread(free, policy, n)]


def _set(event_id: str, **cols: Any) -> None:
    cols["updated_at"] = now_iso()
    sets = ",".join(f"{k}=?" for k in cols)
    run(f"UPDATE bookings SET {sets} WHERE event_id=?", (*cols.values(), event_id))


def _row(event_id: str) -> dict[str, Any] | None:
    r = q1("SELECT * FROM bookings WHERE event_id=?", (event_id,))
    return dict(r) if r else None


def _reconcile(lead_id: int, rep: Rep, event_id: str) -> BookResult:
    """Ask Google what really exists for this event id. Safe: read-only."""
    try:
        ev = google.get_event(rep.calendar_id, event_id)
    except google.AuthExpired as e:
        set_health("calendar", "expired", str(e))
        _set(event_id, status="uncertain", error=f"reconcile failed: {e}")
        return BookResult("uncertain", "Could not verify the booking: Google connection expired. Reconnect Google, then press Reconcile.", _row(event_id))
    except (google.ApiError, google.Uncertain) as e:
        _set(event_id, status="uncertain", error=f"reconcile failed: {e}")
        return BookResult("uncertain", f"Could not verify whether the event exists ({e}). Not retrying automatically.", _row(event_id))
    if ev:
        _set(event_id, status="confirmed", html_link=ev.get("htmlLink"), error=None)
        log(lead_id, "booking_reconciled", f"event {event_id} exists on {rep.name}'s calendar; marked confirmed")
        return BookResult("confirmed", "Booking confirmed (verified on calendar).", _row(event_id))
    _set(event_id, status="failed", error="verified: event does not exist on the calendar")
    log(lead_id, "booking_reconciled", f"event {event_id} does NOT exist; safe to attempt again on explicit confirmation", "warn")
    return BookResult("failed", "The earlier attempt did not create an event (verified). It can be attempted again on explicit confirmation.", _row(event_id))


def reconcile(booking_id: int) -> BookResult | None:
    b = q1("SELECT * FROM bookings WHERE id=?", (booking_id,))
    if not b:
        return None
    from .policy import load_policy
    rep = load_policy().rep(b["rep_id"])
    if not rep:
        return BookResult("failed", "rep no longer exists in policy", dict(b))
    return _reconcile(b["lead_id"], rep, b["event_id"])


def book(policy: Policy, lead: dict[str, Any], rep: Rep, start_iso: str, end_iso: str, now: datetime | None = None) -> BookResult:
    lead_id = lead["id"]
    event_id = event_id_for(lead_id, rep.id, start_iso)
    now_s = now_iso()

    other = q1("SELECT * FROM bookings WHERE lead_id=? AND status='confirmed' AND event_id<>?", (lead_id, event_id))
    if other:
        return BookResult("lead_already_booked", "This lead already has a confirmed meeting; not creating a second one.", dict(other))

    cur = get_conn().execute(
        "INSERT OR IGNORE INTO bookings(lead_id,rep_id,start_iso,end_iso,event_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (lead_id, rep.id, start_iso, end_iso, event_id, "booking", now_s, now_s),
    )
    if cur.rowcount == 0:  # we've seen this exact (lead, rep, slot) before
        row = _row(event_id) or {}
        st = row.get("status")
        if st == "confirmed":
            log(lead_id, "duplicate_confirmation", f"event {event_id} already confirmed; no second event created")
            return BookResult("already_confirmed", "Already booked; duplicate confirmation ignored.", row)
        if st == "uncertain":
            return _reconcile(lead_id, rep, event_id)
        if st == "booking":
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"])).total_seconds()
            if age < 120:
                return BookResult("in_progress", "A booking for this slot is already in progress.", row)
            return _reconcile(lead_id, rep, event_id)   # stale in-flight attempt (crash) -> verify, don't re-insert
        # failed / slot_taken: previous attempt provably did not create the event; claim a fresh attempt atomically
        claimed = get_conn().execute("UPDATE bookings SET status='booking', error=NULL, updated_at=? WHERE event_id=? AND status IN ('failed','slot_taken')",
                                     (now_s, event_id)).rowcount
        if claimed == 0:
            return BookResult("in_progress", "Another attempt just started for this slot.", _row(event_id))

    # ---- recheck availability immediately before writing
    try:
        busy = google.freebusy([rep.calendar_id], datetime.fromisoformat(start_iso) - timedelta(minutes=1), datetime.fromisoformat(end_iso) + timedelta(minutes=1))[rep.calendar_id]
    except google.AuthExpired as e:
        set_health("calendar", "expired", str(e))
        _set(event_id, status="failed", error=str(e))
        log(lead_id, "booking_failed", f"calendar connection expired: {e}", "error")
        return BookResult("auth_expired", "Calendar connection expired; nothing was booked.", _row(event_id))
    except google.ApiError as e:
        set_health("calendar", "error", str(e))
        _set(event_id, status="failed", error=str(e))
        log(lead_id, "booking_failed", f"availability recheck failed: {e}", "error")
        return BookResult("failed", f"Could not re-check availability ({e}); nothing was booked.", _row(event_id))
    slot = (datetime.fromisoformat(start_iso), datetime.fromisoformat(end_iso))
    if overlaps(slot, busy) or overlaps(slot, _internal_busy(rep.id, event_id)):
        _set(event_id, status="slot_taken", error="slot became busy before booking")
        log(lead_id, "slot_taken", f"{start_iso} on {rep.name}'s calendar was taken before confirmation; offering new times", "warn")
        return BookResult("slot_taken", "That time was just taken.", _row(event_id))

    # ---- write (single attempt)
    f = lead.get("fields", {})
    body: dict[str, Any] = {
        "id": event_id,
        "summary": f"Intro call: {f.get('company_name') or lead.get('sender_name') or lead['sender_id']} ({policy.service(f.get('service')).name if policy.service(f.get('service')) else 'enquiry'})",
        "description": f"Booked by the sales assistant for lead #{lead_id} via {lead['channel']}.\nSender: {lead['sender_id']}\nFields: {f}",
        "start": {"dateTime": start_iso, "timeZone": "UTC"},
        "end": {"dateTime": end_iso, "timeZone": "UTC"},
        "extendedProperties": {"private": {"lead_id": str(lead_id), "source": "sales-assistant-prototype"}},
    }
    if lead["channel"] == "email":
        body["attendees"] = [{"email": lead["sender_id"]}]
    try:
        ev = google.insert_event(rep.calendar_id, body)
    except google.AuthExpired as e:
        set_health("calendar", "expired", str(e))
        _set(event_id, status="failed", error=str(e))
        log(lead_id, "booking_failed", f"calendar connection expired: {e}", "error")
        return BookResult("auth_expired", "Calendar connection expired; nothing was booked.", _row(event_id))
    except google.ApiError as e:
        if e.status == 409:   # the id already exists: ours (earlier success we lost track of) or a collision
            try:
                existing = google.get_event(rep.calendar_id, event_id)
            except Exception as ee:  # noqa: BLE001
                _set(event_id, status="uncertain", error=f"409 then verification failed: {ee}")
                return BookResult("uncertain", "Calendar reported a duplicate id but verification failed; needs a human.", _row(event_id))
            if existing and (existing.get("extendedProperties", {}).get("private", {}).get("lead_id") == str(lead_id)):
                _set(event_id, status="confirmed", html_link=existing.get("htmlLink"), error=None)
                log(lead_id, "duplicate_confirmation", "Calendar answered 409 for our event id; verified it is this lead's event; no duplicate created")
                return BookResult("already_confirmed", "Already booked.", _row(event_id))
        _set(event_id, status="failed", error=str(e))
        set_health("calendar", "error", str(e))
        log(lead_id, "booking_failed", f"calendar API rejected the booking: {e}", "error")
        return BookResult("failed", f"Calendar rejected the booking ({e}). Nothing was created.", _row(event_id))
    except google.Uncertain as e:
        log(lead_id, "booking_uncertain", f"write outcome unknown ({e}); verifying by event id instead of retrying", "warn")
        return _reconcile(lead_id, rep, event_id)

    _set(event_id, status="confirmed", html_link=ev.get("htmlLink"), error=None)
    set_health("calendar", "ok", "last booking ok")
    log(lead_id, "booking_confirmed", f"event {event_id} created on {rep.name}'s calendar for {start_iso}")
    return BookResult("confirmed", "Booked.", _row(event_id))
