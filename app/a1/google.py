"""Google OAuth + thin REST clients for Calendar and Gmail (plain httpx, no SDK, so failure modes are explicit).

Error taxonomy used by callers:
  AuthExpired - refresh token revoked/expired or missing (visible 'reconnect' state)
  ApiError    - the API answered with an error (status + body). For writes the operation did NOT happen.
  Uncertain   - a write may or may not have been applied (timeout / connection drop / 5xx). Never retry blindly.
"""
from __future__ import annotations

import base64
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import get_settings
from ..db import kv_del, kv_get, kv_set

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
    "openid", "email",
]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GCAL = "https://www.googleapis.com/calendar/v3"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
TIMEOUT = httpx.Timeout(20.0, connect=8.0)


class AuthExpired(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


class Uncertain(Exception):
    pass


_http: httpx.Client | None = None
_tok: dict[str, Any] = {"access": None, "exp": 0.0}


def _client() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=TIMEOUT)
    return _http


def set_http_client(c: httpx.Client | None) -> None:
    """Tests inject an httpx.Client with a MockTransport."""
    global _http
    _http = c
    _tok.update(access=None, exp=0.0)


# ------------------------------------------------------------------ OAuth


def configured() -> bool:
    s = get_settings()
    return bool(s.google_client_id and s.google_client_secret)


def redirect_uri() -> str:
    return get_settings().base_url.rstrip("/") + "/a1/auth/google/callback"


def auth_url(state: str) -> str:
    s = get_settings()
    q = {"client_id": s.google_client_id, "redirect_uri": redirect_uri(), "response_type": "code", "scope": " ".join(SCOPES),
         "access_type": "offline", "prompt": "consent", "state": state, "include_granted_scopes": "true"}
    return AUTH_URL + "?" + urlencode(q)


def exchange_code(code: str) -> dict[str, Any]:
    s = get_settings()
    r = _client().post(TOKEN_URL, data={"code": code, "client_id": s.google_client_id, "client_secret": s.google_client_secret,
                                        "redirect_uri": redirect_uri(), "grant_type": "authorization_code"})
    if r.status_code != 200:
        raise ApiError(r.status_code, r.text)
    data = r.json()
    if data.get("refresh_token"):
        kv_set("google_refresh_token", data["refresh_token"])
    _tok.update(access=data["access_token"], exp=time.time() + data.get("expires_in", 3600) - 60)
    kv_del("google_expired")
    return data


def connected() -> bool:
    return bool(kv_get("google_refresh_token")) and not kv_get("google_expired")


def disconnect() -> None:
    kv_del("google_refresh_token")
    kv_del("google_account_email")
    _tok.update(access=None, exp=0.0)


def _refresh() -> str:
    s = get_settings()
    rt = kv_get("google_refresh_token")
    if not rt:
        raise AuthExpired("Google is not connected")
    try:
        r = _client().post(TOKEN_URL, data={"client_id": s.google_client_id, "client_secret": s.google_client_secret,
                                            "refresh_token": rt, "grant_type": "refresh_token"})
    except httpx.HTTPError as e:
        raise ApiError(0, f"token endpoint unreachable: {e}") from e
    if r.status_code == 200:
        d = r.json()
        _tok.update(access=d["access_token"], exp=time.time() + d.get("expires_in", 3600) - 60)
        return d["access_token"]
    body = r.text
    if r.status_code in (400, 401) and "invalid_grant" in body:
        kv_set("google_expired", "1")
        raise AuthExpired("Google refresh token is expired or revoked (invalid_grant). Reconnect Google.")
    raise ApiError(r.status_code, body)


def access_token() -> str:
    if _tok["access"] and time.time() < _tok["exp"]:
        return _tok["access"]
    return _refresh()


def _req(method: str, url: str, *, write: bool = False, **kw: Any) -> httpx.Response:
    """One authenticated request. A 401 means the call was rejected before executing, so a single re-auth + resend is safe
    even for writes. Transport failures on writes raise Uncertain (the server may have applied the change)."""
    for attempt in (0, 1):
        headers = {"Authorization": f"Bearer {access_token()}"}
        try:
            r = _client().request(method, url, headers=headers, **kw)
        except httpx.HTTPError as e:
            if write:
                raise Uncertain(f"{type(e).__name__}: {e}") from e
            raise ApiError(0, f"{type(e).__name__}: {e}") from e
        if r.status_code == 401 and attempt == 0:
            _tok.update(access=None, exp=0.0)
            continue
        if r.status_code == 401:
            kv_set("google_expired", "1")
            raise AuthExpired("Google rejected the access token after refresh; reconnect Google.")
        if write and r.status_code >= 500:
            raise Uncertain(f"HTTP {r.status_code}: {r.text[:200]}")
        return r
    raise AssertionError("unreachable")


# ------------------------------------------------------------------ Calendar


def _rfc3339(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def freebusy(calendar_ids: list[str], t_min: datetime, t_max: datetime) -> dict[str, list[tuple[datetime, datetime]]]:
    r = _req("POST", f"{GCAL}/freeBusy", json={"timeMin": _rfc3339(t_min), "timeMax": _rfc3339(t_max),
                                                "items": [{"id": c} for c in calendar_ids]})
    if r.status_code != 200:
        raise ApiError(r.status_code, r.text)
    out: dict[str, list[tuple[datetime, datetime]]] = {}
    for cid, info in r.json().get("calendars", {}).items():
        if info.get("errors"):
            raise ApiError(404, f"calendar {cid}: {info['errors']}")
        out[cid] = [(datetime.fromisoformat(b["start"].replace("Z", "+00:00")), datetime.fromisoformat(b["end"].replace("Z", "+00:00")))
                    for b in info.get("busy", [])]
    return out


def insert_event(calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Raises ApiError(409) for a duplicate event id, ApiError for other 4xx (not applied), Uncertain on timeout/5xx."""
    r = _req("POST", f"{GCAL}/calendars/{calendar_id}/events", write=True, params={"sendUpdates": "none"}, json=body)
    if r.status_code == 200:
        return r.json()
    raise ApiError(r.status_code, r.text)


def get_event(calendar_id: str, event_id: str) -> dict[str, Any] | None:
    r = _req("GET", f"{GCAL}/calendars/{calendar_id}/events/{event_id}")
    if r.status_code == 200:
        ev = r.json()
        return None if ev.get("status") == "cancelled" else ev
    if r.status_code in (404, 410):
        return None
    raise ApiError(r.status_code, r.text)


# ------------------------------------------------------------------ Gmail


def profile_email() -> str:
    cached = kv_get("google_account_email")
    if cached:
        return cached
    r = _req("GET", f"{GMAIL}/profile")
    if r.status_code != 200:
        raise ApiError(r.status_code, r.text)
    email = r.json()["emailAddress"].lower()
    kv_set("google_account_email", email)
    return email


def gmail_list_unread(max_results: int = 20) -> list[str]:
    r = _req("GET", f"{GMAIL}/messages", params={"q": "in:inbox is:unread newer_than:3d", "maxResults": max_results})
    if r.status_code != 200:
        raise ApiError(r.status_code, r.text)
    return [m["id"] for m in r.json().get("messages", [])]


def _decode(b64: str) -> str:
    return base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8", errors="replace")


def _body_text(payload: dict[str, Any]) -> str:
    import re
    plain, html = [], []

    def walk(p: dict[str, Any]) -> None:
        mt = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data and mt == "text/plain":
            plain.append(_decode(data))
        elif data and mt == "text/html":
            html.append(_decode(data))
        for sub in p.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        text = "\n".join(plain)
    else:
        text = re.sub(r"<[^>]+>", " ", "\n".join(html))
    # drop quoted reply history ("On ... wrote:" and '>' lines) so the model sees only the new text
    lines = []
    for ln in text.splitlines():
        if re.match(r"^\s*On .+ wrote:\s*$", ln) or ln.strip().startswith(">"):
            break
        lines.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def gmail_get(msg_id: str) -> dict[str, Any]:
    r = _req("GET", f"{GMAIL}/messages/{msg_id}", params={"format": "full"})
    if r.status_code != 200:
        raise ApiError(r.status_code, r.text)
    m = r.json()
    h = {x["name"].lower(): x["value"] for x in m["payload"].get("headers", [])}
    return {"id": m["id"], "thread_id": m["threadId"], "headers": h, "text": _body_text(m["payload"])}


def gmail_mark_read(msg_id: str) -> None:
    r = _req("POST", f"{GMAIL}/messages/{msg_id}/modify", json={"removeLabelIds": ["UNREAD"]})
    if r.status_code != 200:
        raise ApiError(r.status_code, r.text)


def gmail_send(to: str, subject: str, body: str, thread_id: str | None, in_reply_to: str | None, references: str | None) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg["Auto-Submitted"] = "auto-replied"  # lets other auto-responders ignore us; prevents mail loops
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " if references else "") + in_reply_to
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload: dict[str, Any] = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    r = _req("POST", f"{GMAIL}/messages/send", write=True, json=payload)
    if r.status_code != 200:
        raise ApiError(r.status_code, r.text)
    return r.json()["id"]
