"""Lead lifecycle: inbound -> extraction -> deterministic classification/routing -> slot offer -> confirmation -> booking.

Control flow facts that matter for safety
* Only code decides classification, assignment, which slots exist, and when a booking is created.
* A booking needs (1) an offered slot, (2) the lead picking it, (3) the lead explicitly confirming it. Each step is a
  separate inbound message; the pending slot is stored in the DB, not inferred by the model.
* `automation_enabled` (takeover) is re-read immediately before every automatic send and before every booking.
* Every provider message id is de-duplicated, outbound volume per lead is capped, and automated/no-reply email is ignored.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import get_settings
from ..db import get_conn, jdump, jload, now_iso, q, q1, run
from ..llm import LLMError
from . import booking, google
from .channels import email as email_ch, instagram, whatsapp
from .channels.base import Inbound, SendResult
from .engine import classify, fmt_slot, route
from .extract import (
    OVERRIDE_NOTICE, compose_followup, extract, template_decline, template_followup, verify_fields, merge_fields,
)
from .health import log, set_health
from .policy import Policy, load_policy

logger = logging.getLogger("a1")
_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()
MAX_BOT_MSGS_PER_HOUR = 12

YES = re.compile(r"^\s*(yes|y|yep|yeah|yup|confirm|confirmed|ok|okay|sure|book it|go ahead|sounds good|that works|please do)\W*$", re.I)
NO = re.compile(r"^\s*(none|no|nope|none of (these|them)( work)?|neither)\W*$", re.I)
# Pure acknowledgements ("thanks", "ok", thumbs-up). After the flow has ended they must not restart it.
ACK = re.compile(r"^\s*(?:(?:thanks|thank you|thank u|thx|ty|tysm|many thanks|cheers|ok|okay|k|cool|great|perfect|awesome|nice|got it|noted|alright|sounds good|will do|bye|good bye|goodbye|see you|take care|sure|yes|yep|yeah|yup|fine|no problem|np|\U0001F44D|\U0001F64F|\U0001F44C|\U0001F60A|\U0001F642)[\s.!,]*)+(?:so much|a lot)?[\s.!]*$", re.I)
NUM = re.compile(r"^\s*(?:option|slot|number|no\.?|#)?\s*(\d)\s*[).!]?\s*$", re.I)


def _lock(lead_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(lead_id, threading.Lock())


# ------------------------------------------------------------------ data helpers


def _lead(lead_id: int) -> dict[str, Any]:
    r = q1("SELECT * FROM leads WHERE id=?", (lead_id,))
    d = dict(r)
    d["fields"] = jload(d.pop("fields_json"), {})
    d["evidence"] = jload(d.pop("evidence_json"), [])
    d["rule_trace"] = jload(d.pop("rule_trace_json"), [])
    d["offered_slots"] = jload(d.pop("offered_slots_json"), [])
    d["thread_ref"] = jload(d["thread_ref"], {})
    d["pending_slot"] = jload(d["pending_slot"], None)
    d["automation_enabled"] = bool(d["automation_enabled"])
    return d


def _save(lead_id: int, **cols: Any) -> None:
    mapping = {"fields": "fields_json", "evidence": "evidence_json", "rule_trace": "rule_trace_json", "offered_slots": "offered_slots_json"}
    out = {}
    for k, v in cols.items():
        k2 = mapping.get(k, k)
        if k2.endswith("_json") or k in ("thread_ref", "pending_slot"):
            v = jdump(v) if v is not None else None
        out[k2] = v
    out["updated_at"] = now_iso()
    run(f"UPDATE leads SET {','.join(f'{k}=?' for k in out)} WHERE id=?", (*out.values(), lead_id))


def history(lead_id: int) -> list[dict[str, str]]:
    return [{"author": r["author"], "text": r["text"]} for r in q("SELECT author,text FROM messages WHERE lead_id=? ORDER BY id", (lead_id,))]


def _body_only(text: str, channel: str) -> str:
    if channel == "email":
        return re.sub(r"^Subject:.*?\n\n", "", text, count=1, flags=re.S).strip()
    return text.strip()


def _open_counts() -> dict[str, int]:
    rows = q("SELECT assigned_rep, COUNT(*) n FROM leads WHERE assigned_rep IS NOT NULL AND state IN ('offering_slots','awaiting_confirmation','booked','needs_attention') GROUP BY assigned_rep")
    return {r["assigned_rep"]: r["n"] for r in rows}


def _upsert_lead(inb: Inbound) -> int:
    ts = now_iso()
    get_conn().execute(
        "INSERT OR IGNORE INTO leads(channel,sender_id,sender_name,thread_ref,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (inb.channel, inb.sender_id, inb.sender_name, jdump(inb.thread_ref) if inb.thread_ref else None, ts, ts),
    )
    row = q1("SELECT id FROM leads WHERE channel=? AND sender_id=?", (inb.channel, inb.sender_id))  # identity = (channel, provider id); never merged across channels
    lead_id = row["id"]
    if inb.sender_name or inb.thread_ref:
        run("UPDATE leads SET sender_name=COALESCE(?,sender_name), thread_ref=COALESCE(?,thread_ref) WHERE id=?",
            (inb.sender_name, jdump(inb.thread_ref) if inb.thread_ref else None, lead_id))
    return lead_id


# ------------------------------------------------------------------ outbound


def _send(lead: dict[str, Any], text: str, author: str = "bot") -> bool:
    """Send via the originating channel. Re-checks takeover for automatic sends. Never auto-retries."""
    if author == "bot":
        if not _lead(lead["id"])["automation_enabled"]:
            log(lead["id"], "skipped", "automatic reply suppressed: human takeover is active", "warn")
            return False
        recent = q1("SELECT COUNT(*) n FROM messages WHERE lead_id=? AND author='bot' AND created_at>?",
                    (lead["id"], (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")))["n"]
        if recent >= MAX_BOT_MSGS_PER_HOUR:
            _save(lead["id"], automation_enabled=0)
            log(lead["id"], "loop_guard", f"{recent} automatic messages in the last hour; automation paused to prevent a reply loop", "error")
            return False
    ch = lead["channel"]
    if ch == "whatsapp":
        res = whatsapp.send(lead["sender_id"], text)
    elif ch == "instagram":
        res = instagram.send(lead["sender_id"], text)
    elif ch == "email":
        res = email_ch.send(lead["sender_id"], text, _lead(lead["id"])["thread_ref"])
    else:
        res = SendResult(False, error=f"unknown channel {ch}")
    delivery = "sent" if res.ok else ("uncertain" if res.uncertain else "failed")
    run("INSERT INTO messages(lead_id,direction,author,text,provider_msg_id,delivery,delivery_error,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (lead["id"], "out", author, text, None, delivery, res.error, now_iso()))
    if res.ok:
        set_health(ch, "ok", "last send ok", outbound=True)
        log(lead["id"], "reply_sent", f"{author} message sent via {ch}")
    else:
        set_health(ch, "expired" if res.expired else "error", res.error)
        log(lead["id"], "send_failed", f"{ch} send {delivery}: {res.error}" + (" (not retried: delivery state unknown)" if res.uncertain else ""), "error")
    return res.ok


# ------------------------------------------------------------------ inbound


def handle_inbound(inb: Inbound) -> str:
    """Entry point for webhooks and the Gmail poller. Idempotent per provider message id."""
    cur = get_conn().execute("INSERT OR IGNORE INTO seen_events(channel,provider_msg_id,created_at) VALUES(?,?,?)", (inb.channel, inb.provider_msg_id, now_iso()))
    if cur.rowcount == 0:
        return "duplicate"
    set_health(inb.channel, "ok", "inbound received", inbound=True)
    lead_id = _upsert_lead(inb)
    with _lock(lead_id):
        run("INSERT INTO messages(lead_id,direction,author,text,provider_msg_id,delivery,created_at) VALUES(?,?,?,?,?,?,?)",
            (lead_id, "in", "lead", inb.text, inb.provider_msg_id, "n/a", now_iso()))
        run("UPDATE leads SET last_inbound_at=? WHERE id=?", (now_iso(), lead_id))
        log(lead_id, "inbound", f"{inb.channel} message from {inb.sender_name or inb.sender_id}: {inb.text[:80]!r}")
        lead = _lead(lead_id)
        if not lead["automation_enabled"]:
            log(lead_id, "skipped", "message stored; automation is paused (human takeover), no automatic action taken", "warn")
            return "paused"
        _process(lead_id)
    return "processed"


def retry(lead_id: int) -> None:
    with _lock(lead_id):
        lead = _lead(lead_id)
        if not lead["automation_enabled"]:
            log(lead_id, "skipped", "retry refused: human takeover is active", "warn")
            return
        log(lead_id, "retry", "operator requested a retry of the last automatic step")
        _process(lead_id)


def _deterministic(text: str, offered: list, pending: Any, state: str) -> tuple[str, int | None] | None:
    t = text
    if state == "awaiting_confirmation" and YES.match(t):
        return "confirm", None
    m = NUM.match(t)
    if m and state in ("offering_slots", "awaiting_confirmation") and 1 <= int(m.group(1)) <= len(offered):
        return "choose", int(m.group(1))
    if NO.match(t):
        return "decline", None
    return None


def _process(lead_id: int) -> None:
    lead = _lead(lead_id)
    policy = load_policy()
    msgs = history(lead_id)
    last = next((m["text"] for m in reversed(msgs) if m["author"] == "lead"), "")
    last_body = _body_only(last, lead["channel"])
    state = lead["state"]

    if state in ("booked", "needs_attention") and ACK.match(last_body):
        # the conversation already reached its end (confirmed or handed to a human): a thank-you must not reopen it
        log(lead_id, "ack", f"acknowledgement received in state {state}; state unchanged, nothing re-offered")
        if state == "booked":
            _send(lead, "You're welcome! See you at the meeting.")
        else:
            rep = policy.rep(lead["assigned_rep"]) if lead["assigned_rep"] else None
            _send(lead, f"You're welcome! {rep.name if rep else 'A team member'} will be in touch.")
        return
    if state == "booked":
        b = q1("SELECT * FROM bookings WHERE lead_id=? AND status='confirmed'", (lead_id,))
        when = fmt_slot(b["start_iso"], b["end_iso"], policy) if b else "the agreed time"
        _send(lead, f"Your meeting is confirmed for {when}. A team member will follow up on anything else.")
        return
    if state == "needs_attention":
        unresolved = q1("SELECT 1 FROM bookings WHERE lead_id=? AND status='uncertain'", (lead_id,))
        if unresolved:
            log(lead_id, "skipped", "a booking is in an uncertain state; use Reconcile before any further automatic action", "warn")
            return
        state = "collecting"

    offered = [fmt_slot(s["start"], s["end"], policy) for s in lead["offered_slots"]]
    pending_txt = fmt_slot(lead["pending_slot"]["start"], lead["pending_slot"]["end"], policy) if lead["pending_slot"] else None

    det = _deterministic(last_body, lead["offered_slots"], lead["pending_slot"], state) if state in ("offering_slots", "awaiting_confirmation") else None
    intent, choice, override, ex = (det[0], det[1], False, None) if det else (None, None, False, None)

    if det is None:
        lead_texts = [m["text"] for m in msgs if m["author"] == "lead"]
        try:
            ex = extract(f"a1:lead:{lead_id}", policy, msgs, offered, pending_txt)
        except LLMError as e:
            log(lead_id, "llm_error", f"extraction failed: {e}. No automatic reply was sent; retry from the dashboard or take over.", "error")
            return
        fields, evidence, dropped = verify_fields(ex, policy, lead_texts)
        fields, evidence = merge_fields(lead["fields"], lead["evidence"], fields, evidence, len(lead_texts))
        for d in dropped:
            log(lead_id, "ungrounded", d, "warn")
        cls = classify(fields, policy)
        _save(lead_id, fields=fields, evidence=evidence, classification=cls.label, rule_trace=cls.trace)
        override = ex.policy_override_attempt
        if override:
            log(lead_id, "override_attempt", "lead tried to change policy/rules; ignored. Classification and routing come from the policy engine only.", "warn")
        intent, choice = ex.intent, ex.slot_choice
        lead = _lead(lead_id)
        if state in ("offering_slots", "awaiting_confirmation"):
            # the model may re-interpret a free-text reply as a slot choice/confirmation, but only valid indexes count
            if intent == "choose_slot" and choice and 1 <= choice <= len(lead["offered_slots"]):
                intent = "choose"
            elif intent == "confirm" and state == "awaiting_confirmation":
                intent = "confirm"
            elif intent == "decline":
                intent = "decline"
            else:
                intent = None
        else:
            intent = None
        if cls.label != "qualified":
            _unqualify(lead, cls, msgs, override, policy)
            return
    else:
        cls = classify(lead["fields"], policy)

    prefix = OVERRIDE_NOTICE if override else ""
    lead = _lead(lead_id)

    if intent == "choose" and choice:
        _choose(lead, choice, policy, prefix)
    elif intent == "confirm":
        _confirm(lead, policy)
    elif intent == "decline":
        _decline_slots(lead, policy)
    else:
        _qualified_flow(lead, cls, policy, prefix)


def _unqualify(lead: dict[str, Any], cls, msgs: list[dict[str, str]], override: bool, policy: Policy) -> None:
    lead_id = lead["id"]
    if lead["state"] in ("offering_slots", "awaiting_confirmation"):
        _save(lead_id, state="collecting", pending_slot=None, offered_slots=[], assigned_rep=None)
    else:
        _save(lead_id, assigned_rep=None)
    prefix = OVERRIDE_NOTICE if override else ""
    if cls.label == "not_qualified":
        log(lead_id, "classified", "not_qualified: " + "; ".join(cls.reasons))
        _send(_lead(lead_id), prefix + template_decline(policy, cls.reasons))
        return
    log(lead_id, "classified", f"needs_more_info: missing {', '.join(cls.missing)}")
    try:
        body = compose_followup(f"a1:lead:{lead_id}", policy, lead["channel"], msgs, cls.missing)
    except Exception as e:  # noqa: BLE001 - any failure falls back to the deterministic template
        log(lead_id, "compose_fallback", f"LLM follow-up unavailable or rejected ({type(e).__name__}); used template", "warn")
        body = template_followup(policy, cls.missing)
    _send(_lead(lead_id), prefix + body)


def _qualified_flow(lead: dict[str, Any], cls, policy: Policy, prefix: str) -> None:
    lead_id = lead["id"]
    if cls.label != "qualified":
        # reached via a deterministic path with stale fields; nothing to do
        return
    rep_id, rtrace = route(lead["fields"], policy, {k: v - (1 if k == lead["assigned_rep"] else 0) for k, v in _open_counts().items()})
    rep = policy.rep(rep_id)
    changed = lead["assigned_rep"] != rep_id
    _save(lead_id, assigned_rep=rep_id, rule_trace=lead["rule_trace"] + [{"rule": "ROUTING", **t} for t in rtrace])
    if changed:
        log(lead_id, "routed", f"assigned to {rep.name} by " + next((t["rule"] for t in rtrace if t["result"] == "match"), "?"))
    if lead["state"] in ("offering_slots", "awaiting_confirmation") and not changed:
        policy_lines = "\n".join(f"{i}) {fmt_slot(s['start'], s['end'], policy)}" for i, s in enumerate(lead["offered_slots"], 1))
        if lead["state"] == "awaiting_confirmation":
            _send(lead, prefix + "Your selected time is waiting. Reply YES to confirm it, or reply with another number:\n" + policy_lines)
        else:
            _send(lead, prefix + "Please reply with the number of a time that suits you:\n" + policy_lines + "\n(Or reply NONE if none of these work.)")
        return
    _offer_slots(_lead(lead_id), policy, prefix)


def _offer_slots(lead: dict[str, Any], policy: Policy, prefix: str = "", intro: bool = True) -> None:
    lead_id = lead["id"]
    rep = policy.rep(lead["assigned_rep"])
    try:
        slots = booking.available_slots(policy, rep, 3)
    except google.AuthExpired as e:
        _save(lead_id, state="needs_attention", offered_slots=[], pending_slot=None)
        log(lead_id, "calendar_error", f"cannot read availability: {e}. No message sent to the lead. Reconnect Google, then Retry.", "error")
        return
    except (google.ApiError, google.Uncertain) as e:
        _save(lead_id, state="needs_attention", offered_slots=[], pending_slot=None)
        log(lead_id, "calendar_error", f"cannot read availability: {e}. No message sent to the lead; use Retry or take over.", "error")
        return
    if not slots:
        _save(lead_id, state="needs_attention", offered_slots=[], pending_slot=None)
        log(lead_id, "no_availability", f"{rep.name} has no free slots in the next {policy.lookahead_days} days", "warn")
        _send(lead, prefix + f"Thanks, that's everything I need. {rep.name} will reach out personally to find a time.")
        return
    offered = [{"start": s, "end": e} for s, e in slots]
    _save(lead_id, state="offering_slots", offered_slots=offered, pending_slot=None)
    lines = "\n".join(f"{i}) {fmt_slot(s['start'], s['end'], policy)}" for i, s in enumerate(offered, 1))
    head = f"Thanks{' ' + lead['fields']['contact_name'] if lead['fields'].get('contact_name') else ''}! " if intro else ""
    _send(_lead(lead_id), f"{prefix}{head}Based on what you've shared, {rep.name} will be your contact at {policy.company}. Which time suits you? Reply with the number:\n{lines}\n(Or reply NONE if none of these work.)")
    log(lead_id, "slots_offered", f"offered {len(offered)} free slots from {rep.name}'s calendar")


def _choose(lead: dict[str, Any], idx: int, policy: Policy, prefix: str) -> None:
    slot = lead["offered_slots"][idx - 1]
    _save(lead["id"], pending_slot=slot, state="awaiting_confirmation")
    log(lead["id"], "slot_selected", f"lead selected option {idx}: {slot['start']}")
    _send(_lead(lead["id"]), f"{prefix}You picked {fmt_slot(slot['start'], slot['end'], policy)}. Reply YES to confirm and I'll book it.")


def _decline_slots(lead: dict[str, Any], policy: Policy) -> None:
    _save(lead["id"], state="needs_attention", pending_slot=None)
    log(lead["id"], "slots_declined", "lead rejected the offered times; handed to a human", "warn")
    rep = policy.rep(lead["assigned_rep"])
    _send(lead, f"No problem. {rep.name if rep else 'A team member'} will reach out to find a time that works.")


def _confirm(lead: dict[str, Any], policy: Policy) -> None:
    lead_id = lead["id"]
    pend = lead["pending_slot"]
    if not pend:
        _send(lead, "I don't have a time waiting for confirmation. Reply with the number of a time to pick one.")
        return
    if not _lead(lead_id)["automation_enabled"]:
        log(lead_id, "skipped", "booking suppressed: human takeover is active", "warn")
        return
    rep = policy.rep(lead["assigned_rep"])
    res = booking.book(policy, lead, rep, pend["start"], pend["end"])
    when = fmt_slot(pend["start"], pend["end"], policy)
    if res.status in ("confirmed", "already_confirmed"):
        _save(lead_id, state="booked")
        link = (res.booking or {}).get("html_link")
        _send(_lead(lead_id), (f"Confirmed: {when} with {rep.name}." if res.status == "confirmed" else f"You're already booked: {when} with {rep.name}. No duplicate was created.") + " Looking forward to it!")
        if link:
            log(lead_id, "calendar_link", link)
    elif res.status == "lead_already_booked":
        _save(lead_id, state="booked")
        _send(_lead(lead_id), "You already have a confirmed meeting with us, so I haven't booked another.")
    elif res.status == "slot_taken":
        _save(lead_id, pending_slot=None)
        _offer_slots(_lead(lead_id), policy, prefix="Sorry, that time was just taken. ", intro=False)
    elif res.status == "in_progress":
        log(lead_id, "skipped", "booking already in progress; no action", "warn")
    elif res.status == "uncertain":
        _save(lead_id, state="needs_attention")
        _send(_lead(lead_id), "I'm verifying your booking and a team member will confirm shortly.")
    else:  # failed / auth_expired
        _save(lead_id, state="needs_attention")
        _send(_lead(lead_id), "I couldn't complete the booking just now. A team member will follow up to confirm a time.")


# ------------------------------------------------------------------ operator actions


def set_automation(lead_id: int, enabled: bool) -> None:
    _save(lead_id, automation_enabled=1 if enabled else 0)
    log(lead_id, "takeover" if not enabled else "resume", "human took over: all automatic replies and bookings stopped" if not enabled else "automation resumed", "warn" if not enabled else "info")


def human_message(lead_id: int, text: str) -> bool:
    with _lock(lead_id):
        return _send(_lead(lead_id), text, author="human")


def reconcile_booking(lead_id: int) -> str:
    b = q1("SELECT * FROM bookings WHERE lead_id=? AND status IN ('uncertain','failed') ORDER BY id DESC", (lead_id,))
    if not b:
        return "no booking to reconcile"
    res = booking.reconcile(b["id"])
    if res and res.status == "confirmed":
        _save(lead_id, state="booked")
    return res.message if res else "booking not found"


# ------------------------------------------------------------------ Gmail polling


def poll_gmail_once() -> int:
    s = get_settings()
    if not google.configured():
        set_health("email", "unconfigured", "GOOGLE_CLIENT_ID/SECRET not set")
        return 0
    if not google.connected():
        set_health("email", "expired" if q1("SELECT 1 FROM kv WHERE key='google_expired'") else "unconfigured", "Google not connected")
        return 0
    allowed = {a.strip().lower() for a in s.email_allowed_senders.split(",") if a.strip()}
    n = 0
    try:
        own = google.profile_email()
        for mid in google.gmail_list_unread():
            if q1("SELECT 1 FROM seen_events WHERE channel='email' AND provider_msg_id=?", (mid,)):
                google.gmail_mark_read(mid)
                continue
            msg = google.gmail_get(mid)
            inb, skip = email_ch.to_inbound(msg, own)
            if inb is None:
                get_conn().execute("INSERT OR IGNORE INTO seen_events(channel,provider_msg_id,created_at) VALUES('email',?,?)", (mid, now_iso()))
                log(None, "email_ignored", f"ignored message {mid}: {skip}")
            elif inb.sender_id not in allowed:  # empty allowlist -> nobody is auto-replied to
                get_conn().execute("INSERT OR IGNORE INTO seen_events(channel,provider_msg_id,created_at) VALUES('email',?,?)", (mid, now_iso()))
                log(None, "email_ignored", f"ignored message from {inb.sender_id}: sender not in EMAIL_ALLOWED_SENDERS (authorised test senders only)", "warn")
            else:
                handle_inbound(inb)
                n += 1
            google.gmail_mark_read(mid)
        set_health("email", "ok", "polled")
    except google.AuthExpired as e:
        set_health("email", "expired", str(e))
        set_health("calendar", "expired", str(e))
        logger.warning("gmail poll: %s", e)
    except (google.ApiError, google.Uncertain) as e:
        set_health("email", "error", str(e))
        logger.warning("gmail poll error: %s", e)
    return n


async def gmail_poll_loop() -> None:
    interval = max(10, get_settings().gmail_poll_seconds)
    while True:
        try:
            await asyncio.get_running_loop().run_in_executor(None, poll_gmail_once)
        except Exception:  # noqa: BLE001
            logger.exception("gmail poll crashed; continuing")
        await asyncio.sleep(interval)


# ------------------------------------------------------------------ read model


def lead_summary_rows() -> list[dict[str, Any]]:
    rows = q("""SELECT l.*, (SELECT text FROM messages m WHERE m.lead_id=l.id ORDER BY m.id DESC LIMIT 1) AS last_text,
                (SELECT COUNT(*) FROM actions a WHERE a.lead_id=l.id AND a.level='error' AND a.id > COALESCE((SELECT MAX(id) FROM actions r WHERE r.lead_id=l.id AND r.kind IN ('reply_sent','booking_confirmed','retry')),0)) AS open_errors
                FROM leads l ORDER BY l.updated_at DESC""")
    return [{"id": r["id"], "channel": r["channel"], "sender": r["sender_name"] or r["sender_id"], "sender_id": r["sender_id"],
             "classification": r["classification"], "state": r["state"], "assigned_rep": r["assigned_rep"],
             "automation_enabled": bool(r["automation_enabled"]), "last_text": (r["last_text"] or "")[:90], "updated_at": r["updated_at"],
             "open_errors": r["open_errors"]} for r in rows]


def lead_detail(lead_id: int) -> dict[str, Any] | None:
    if not q1("SELECT 1 FROM leads WHERE id=?", (lead_id,)):
        return None
    policy = load_policy()
    lead = _lead(lead_id)
    rep = policy.rep(lead["assigned_rep"])
    bookings = [dict(b) for b in q("SELECT * FROM bookings WHERE lead_id=? ORDER BY id", (lead_id,))]
    for b in bookings:
        b["display"] = fmt_slot(b["start_iso"], b["end_iso"], policy)
    return {
        "lead": {k: lead[k] for k in ("id", "channel", "sender_id", "sender_name", "classification", "state", "assigned_rep", "automation_enabled", "fields", "evidence", "rule_trace", "created_at", "updated_at")},
        "rep_name": rep.name if rep else None,
        "offered": [fmt_slot(s["start"], s["end"], policy) for s in lead["offered_slots"]],
        "pending": fmt_slot(lead["pending_slot"]["start"], lead["pending_slot"]["end"], policy) if lead["pending_slot"] else None,
        "messages": [dict(m) for m in q("SELECT id,direction,author,text,delivery,delivery_error,created_at FROM messages WHERE lead_id=? ORDER BY id", (lead_id,))],
        "bookings": bookings,
        "actions": [dict(a) for a in q("SELECT level,kind,detail,created_at FROM actions WHERE lead_id=? ORDER BY id DESC LIMIT 60", (lead_id,))],
        "usage": dict(q1("SELECT COUNT(*) calls, COALESCE(SUM(cost_usd),0) cost FROM llm_calls WHERE scope=?", (f"a1:lead:{lead_id}",))),
    }


def reset_lead(lead_id: int) -> bool:
    """Operator action for demos and retakes: remove a lead and its messages, bookings and actions so the same
    sender starts a fresh lead. The Google Calendar event is NOT touched (it is the calendar's record), and
    seen_events is kept so already-processed provider messages are never replayed."""
    with _lock(lead_id):
        lead = q1("SELECT sender_id, channel FROM leads WHERE id=?", (lead_id,))
        if not lead:
            return False
        for table in ("messages", "bookings", "actions"):
            run(f"DELETE FROM {table} WHERE lead_id=?", (lead_id,))
        run("DELETE FROM leads WHERE id=?", (lead_id,))
    log(None, "lead_reset", f"operator reset lead #{lead_id} ({lead['channel']}: {lead['sender_id']}); calendar events left in place", "warn")
    return True
