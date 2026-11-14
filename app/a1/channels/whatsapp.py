"""WhatsApp Cloud API (Meta). Test phone number + allow-listed recipients; webhook with HMAC verification.

Constraint worth knowing: free-form (non-template) messages only deliver inside the 24h customer-service window that
opens when the user messages us. Replies here are always responses to an inbound message, so they fall inside it."""
from __future__ import annotations

from typing import Any

from ...config import get_settings
from .base import Inbound, SendResult, graph_post


def configured() -> bool:
    s = get_settings()
    return bool(s.wa_access_token and s.wa_phone_number_id and s.wa_app_secret and s.wa_verify_token)


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
    if not configured():
        return SendResult(False, error="WhatsApp is not configured (set WA_* env vars)")
    url = f"https://graph.facebook.com/{s.wa_graph_version}/{s.wa_phone_number_id}/messages"
    return graph_post(url, s.wa_access_token, {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4000]}})
