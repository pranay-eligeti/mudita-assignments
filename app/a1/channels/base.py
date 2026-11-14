from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any

import httpx

TIMEOUT = httpx.Timeout(15.0, connect=8.0)


@dataclass
class Inbound:
    channel: str
    sender_id: str
    text: str
    provider_msg_id: str
    sender_name: str | None = None
    thread_ref: dict[str, Any] = field(default_factory=dict)  # e.g. email threadId / message-id / subject


@dataclass
class SendResult:
    ok: bool
    provider_id: str | None = None
    error: str | None = None
    expired: bool = False       # credentials rejected -> show 'reconnect' state
    uncertain: bool = False     # transport failure: may or may not have been delivered


def verify_meta_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    """Meta signs webhook bodies: X-Hub-Signature-256: sha256=<hex hmac of raw body with the app secret>."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


_http: httpx.Client | None = None


def http() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=TIMEOUT)
    return _http


def set_http(c: httpx.Client | None) -> None:
    global _http
    _http = c


def graph_post(url: str, token: str, body: dict[str, Any]) -> SendResult:
    """POST to a Meta Graph endpoint and translate the failure modes we care about."""
    try:
        r = http().post(url, headers={"Authorization": f"Bearer {token}"}, json=body)
    except httpx.HTTPError as e:
        return SendResult(False, error=f"transport error: {type(e).__name__}: {e}", uncertain=True)
    if r.status_code == 200:
        d = r.json()
        pid = (d.get("messages") or [{}])[0].get("id") or d.get("message_id")
        return SendResult(True, provider_id=pid)
    try:
        err = r.json().get("error", {})
    except ValueError:
        err = {}
    code = err.get("code")
    msg = f"HTTP {r.status_code} code={code} sub={err.get('error_subcode')}: {err.get('message', r.text[:200])}"
    if r.status_code >= 500:
        return SendResult(False, error=msg, uncertain=True)
    return SendResult(False, error=msg, expired=(code == 190 or r.status_code == 401))
