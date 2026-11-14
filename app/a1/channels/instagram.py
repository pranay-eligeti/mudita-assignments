"""Instagram messaging via 'Instagram API with Instagram Login' (graph.instagram.com).

Setup constraints (from Meta docs): the Instagram account must be a professional account; in development mode only
accounts holding the app's *Instagram Tester* role (accepted in-app) can message it, and Meta states the app must be
published (Live) to receive messaging webhooks. Message text limit is 1000 bytes."""
from __future__ import annotations

from typing import Any

from ...config import get_settings
from .base import Inbound, SendResult, graph_post


def configured() -> bool:
    s = get_settings()
    return bool(s.ig_access_token and s.ig_account_id and s.ig_app_secret and s.ig_verify_token)


def parse(payload: dict[str, Any]) -> list[Inbound]:
    s = get_settings()
    out: list[Inbound] = []
    events: list[dict[str, Any]] = []
    for entry in payload.get("entry", []):
        events.extend(entry.get("messaging", []) or [])
        for ch in entry.get("changes", []) or []:      # alternate envelope used by some app configurations
            if ch.get("field") == "messages" and isinstance(ch.get("value"), dict):
                events.append(ch["value"])
    for ev in events:
        msg = ev.get("message") or {}
        sender = (ev.get("sender") or {}).get("id")
        if not sender or not msg.get("mid"):
            continue
        if msg.get("is_echo") or sender == s.ig_account_id:   # our own outbound messages are echoed back; never treat as inbound
            continue
        text = msg.get("text") or ("[unsupported attachment]" if msg.get("attachments") else "")
        out.append(Inbound("instagram", sender, text, msg["mid"], None))
    return out


def send(to_igsid: str, text: str) -> SendResult:
    s = get_settings()
    if not configured():
        return SendResult(False, error="Instagram is not configured (set IG_* env vars)")
    t = text
    while len(t.encode()) > 1000:      # Meta limit; our templates stay well below, this is a guard
        t = t[:-10]
    url = f"https://graph.instagram.com/{s.ig_graph_version}/{s.ig_account_id}/messages"
    return graph_post(url, s.ig_access_token, {"recipient": {"id": to_igsid}, "message": {"text": t}})
