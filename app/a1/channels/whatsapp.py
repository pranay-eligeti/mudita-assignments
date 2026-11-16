"""WhatsApp, through one of two providers: Twilio (Sandbox or registered sender) or the Meta Cloud API.
Twilio is used when TWILIO_* is configured, otherwise Meta. Both feed the same 'whatsapp' channel and sender ids are
plain digits (no '+', no 'whatsapp:' prefix) so a lead is the same lead whichever provider delivered the message.

Meta Cloud API: test phone number + allow-listed recipients; webhook with HMAC verification.

Constraint worth knowing: free-form (non-template) messages only deliver inside the 24h customer-service window that
opens when the user messages us. Replies here are always responses to an inbound message, so they fall inside it."""
from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import re
from typing import Any

import httpx

from ...config import get_settings
from .base import Inbound, SendResult, graph_post, http


class Capture:
    """While a Twilio webhook is being answered, replies are collected here and returned inline as TwiML instead of being
    sent through the REST API. Twilio trial WhatsApp numbers accept replies this way; the REST API refuses free-form text
    on them (error 21654). Once the webhook has answered (or timed out) "closed" is set and later sends use the REST API."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.closed = False


_capture: contextvars.ContextVar[Capture | None] = contextvars.ContextVar("wa_capture", default=None)


def start_capture(c: Capture) -> contextvars.Token:
    return _capture.set(c)


def end_capture(tok: contextvars.Token) -> None:
    _capture.reset(tok)


def _twilio_configured() -> bool:
    s = get_settings()
    return bool(s.twilio_account_sid and s.twilio_auth_token and s.twilio_whatsapp_from)


def _meta_configured() -> bool:
    s = get_settings()
    return bool(s.wa_access_token and s.wa_phone_number_id and s.wa_app_secret and s.wa_verify_token)


def provider() -> str | None:
    return "twilio" if _twilio_configured() else ("meta" if _meta_configured() else None)


def configured() -> bool:
    return provider() is not None


def verify_twilio_signature(url: str, params: dict[str, str], header: str | None, auth_token: str) -> bool:
    """Twilio signs the full public URL plus the POST parameters sorted by name: X-Twilio-Signature = base64(HMAC-SHA1)."""
    if not auth_token or not header:
        return False
    data = url + "".join(k + params[k] for k in sorted(params))
    expected = base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected, header)


def _digits(addr: str) -> str:
    return re.sub(r"\D", "", addr.replace("whatsapp:", ""))


def parse_twilio(params: dict[str, str]) -> list[Inbound]:
    sid, frm = params.get("MessageSid") or params.get("SmsMessageSid"), params.get("From", "")
    if not sid or not frm.startswith("whatsapp:") or not _digits(frm):
        return []
    text = params.get("Body", "")
    if int(params.get("NumMedia", "0") or 0) and not text:
        text = "[unsupported media message]"
    return [Inbound("whatsapp", _digits(frm), text, sid, params.get("ProfileName"))]


def _send_twilio(to: str, text: str) -> SendResult:
    s = get_settings()
    frm = s.twilio_whatsapp_from if s.twilio_whatsapp_from.startswith("whatsapp:") else "whatsapp:" + s.twilio_whatsapp_from
    url = f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json"
    try:
        r = http().post(url, auth=(s.twilio_account_sid, s.twilio_auth_token), data={"To": f"whatsapp:+{_digits(to)}", "From": frm, "Body": text[:1500]})
    except httpx.HTTPError as e:
        return SendResult(False, error=f"transport error: {type(e).__name__}: {e}", uncertain=True)
    if r.status_code in (200, 201):
        return SendResult(True, provider_id=r.json().get("sid"))
    try:
        d = r.json()
    except ValueError:
        d = {}
    msg = f"HTTP {r.status_code} code={d.get('code')}: {d.get('message', r.text[:200])}"
    if r.status_code >= 500:
        return SendResult(False, error=msg, uncertain=True)
    return SendResult(False, error=msg, expired=(r.status_code == 401))


def parse(payload: dict[str, Any]) -> list[Inbound]:
    out: list[Inbound] = []
    for entry in payload.get("entry", []):
        for ch in entry.get("changes", []):
            if ch.get("field") != "messages":
                continue
            v = ch.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in v.get("contacts", [])}
            for m in v.get("messages", []):
                wa_id = m.get("from")
                if not wa_id or not m.get("id"):
                    continue
                if m.get("type") == "text":
                    text = (m.get("text") or {}).get("body", "")
                elif m.get("type") == "button":
                    text = (m.get("button") or {}).get("text", "")
                elif m.get("type") == "interactive":
                    i = m.get("interactive") or {}
                    text = ((i.get("button_reply") or i.get("list_reply")) or {}).get("title", "")
                else:
                    text = f"[unsupported {m.get('type')} message]"
                out.append(Inbound("whatsapp", wa_id, text, m["id"], names.get(wa_id)))
    return out


def send(to: str, text: str) -> SendResult:
    s = get_settings()
    cap = _capture.get()
    if cap is not None and not cap.closed and _twilio_configured():
        cap.texts.append(text[:1500])
        return SendResult(True, provider_id="twiml")
    if _twilio_configured():
        return _send_twilio(to, text)
    if not _meta_configured():
        return SendResult(False, error="WhatsApp is not configured (set TWILIO_* or WA_* env vars)")
    url = f"https://graph.facebook.com/{s.wa_graph_version}/{s.wa_phone_number_id}/messages"
    return graph_post(url, s.wa_access_token, {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4000]}})
