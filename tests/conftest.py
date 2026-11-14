"""Test harness. No network, no API keys: the LLM, Google and Meta Graph API are replaced by scripted fakes.
Everything between the HTTP boundary and those fakes (webhooks, signatures, dedup, state machine, booking logic,
SQL) is the real production code."""
from __future__ import annotations

import json
import os
import uuid
import re
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import pytest

from app import config, db, llm
from app.a1 import google
from app.a1.channels import base as chbase
from app.llm import LLMBackend, RawResult, SimulatedModelFailure


class FakeLLM:
    """Scripted backend. handlers[agent](user_prompt, call_index) -> dict. Records every prompt for assertions."""

    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[str, int], dict[str, Any]]] = {}
        self.calls: list[dict[str, str]] = []

    def on(self, agent: str, fn: Callable[[str, int], dict[str, Any]]) -> None:
        self.handlers[agent] = fn

    def count(self, agent: str) -> int:
        return sum(1 for c in self.calls if c["agent"] == agent)

    def prompts(self, agent: str) -> list[str]:
        return [c["user"] for c in self.calls if c["agent"] == agent]

    def complete(self, *, agent: str, system: str, user: str, schema: dict, model: str) -> RawResult:
        idx = self.count(agent)
        self.calls.append({"agent": agent, "user": user, "system": system})
        if agent not in self.handlers:
            raise AssertionError(f"no scripted handler for agent {agent}")
        return RawResult(self.handlers[agent](user, idx), 100, 50)


class FakeGoogle:
    """In-memory Google Calendar + Gmail + token endpoint behind an httpx.MockTransport."""

    def __init__(self) -> None:
        self.busy: list[tuple[datetime, datetime]] = []
        self.events: dict[str, dict] = {}
        self.insert_calls = 0
        self.insert_mode = "ok"          # ok | timeout_created | timeout_not_created | http500_not_created
        self.get_fails = False
        self.token_invalid = False
        self.sent_mail: list[dict] = []
        self.freebusy_calls = 0
        self.inbox: list[dict] = []      # {"id","thread","from","subject","body","extra_headers":{}, "unread":True}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url, method = str(request.url), request.method
        if url.startswith(google.TOKEN_URL):
            if self.token_invalid:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
            return httpx.Response(200, json={"access_token": "fake-access", "expires_in": 3600})
        if url.endswith("/freeBusy"):
            self.freebusy_calls += 1
            body = json.loads(request.content)
            cal = body["items"][0]["id"]
            busy = [{"start": s.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": e.strftime("%Y-%m-%dT%H:%M:%SZ")} for s, e in self.busy]
            for ev in self.events.values():
                busy.append({"start": ev["start"]["dateTime"].replace("+00:00", "Z"), "end": ev["end"]["dateTime"].replace("+00:00", "Z")})
            return httpx.Response(200, json={"calendars": {cal: {"busy": busy}}})
        if re.search(r"/calendars/[^/]+/events$", url.split("?")[0]) and method == "POST":
            self.insert_calls += 1
            body = json.loads(request.content)
            eid = body["id"]
            if eid in self.events:
                return httpx.Response(409, json={"error": {"code": 409, "message": "The requested identifier already exists."}})
            if self.insert_mode == "ok":
                self.events[eid] = {**body, "htmlLink": f"https://calendar.example/{eid}", "status": "confirmed"}
                return httpx.Response(200, json=self.events[eid])
            if self.insert_mode == "timeout_created":
                self.events[eid] = {**body, "htmlLink": f"https://calendar.example/{eid}", "status": "confirmed"}
                raise httpx.ReadTimeout("simulated read timeout after server applied the write")
            if self.insert_mode == "timeout_not_created":
                raise httpx.ReadTimeout("simulated timeout before server applied the write")
            if self.insert_mode == "http500_not_created":
                return httpx.Response(503, text="backend error")
        m = re.search(r"/calendars/[^/]+/events/([^/?]+)$", url.split("?")[0])
        if m and method == "GET":
            if self.get_fails:
                return httpx.Response(500, text="boom")
            ev = self.events.get(m.group(1))
            return httpx.Response(200, json=ev) if ev else httpx.Response(404, json={"error": {"code": 404}})
        if method == "GET" and url.split("?")[0].endswith("/gmail/v1/users/me/messages"):
            return httpx.Response(200, json={"messages": [{"id": m["id"]} for m in self.inbox if m["unread"]]})
        gm = re.search(r"/gmail/v1/users/me/messages/([^/?]+)(/modify)?", url)
        if gm and gm.group(1) != "send":
            m = next(x for x in self.inbox if x["id"] == gm.group(1))
            if gm.group(2):
                m["unread"] = False
                return httpx.Response(200, json={"id": m["id"]})
            import base64
            hdrs = [{"name": "From", "value": m["from"]}, {"name": "Subject", "value": m["subject"]}, {"name": "Message-ID", "value": f"<{m['id']}@mail.test>"}]
            hdrs += [{"name": k, "value": v} for k, v in m.get("extra_headers", {}).items()]
            data = base64.urlsafe_b64encode(m["body"].encode()).decode()
            return httpx.Response(200, json={"id": m["id"], "threadId": m["thread"], "payload": {"mimeType": "text/plain", "headers": hdrs, "body": {"data": data}}})
        if url.endswith("/gmail/v1/users/me/messages/send"):
            self.sent_mail.append(json.loads(request.content))
            return httpx.Response(200, json={"id": f"sent{len(self.sent_mail)}"})
        if url.endswith("/gmail/v1/users/me/profile"):
            return httpx.Response(200, json={"emailAddress": "assistant@test.example"})
        raise AssertionError(f"unexpected Google request {method} {url}")


class FakeMeta:
    """Captures outbound Graph API sends (WhatsApp + Instagram)."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail_code: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if self.fail_code:
            return httpx.Response(401 if self.fail_code == 190 else 400, json={"error": {"code": self.fail_code, "message": "simulated"}})
        self.sent.append({"url": str(request.url), "auth": request.headers.get("authorization"), "body": body})
        if "whatsapp" in str(request.url) or "messaging_product" in body:
            return httpx.Response(200, json={"messages": [{"id": f"wamid.out{len(self.sent)}"}]})
        return httpx.Response(200, json={"message_id": f"mid.out{len(self.sent)}"})

    def texts(self) -> list[str]:
        out = []
        for s in self.sent:
            b = s["body"]
            out.append((b.get("text") or {}).get("body") if "text" in b and isinstance(b["text"], dict) else (b.get("message") or {}).get("text"))
        return out


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    pg = os.environ.get("TEST_PG_URL")  # e.g. postgresql://postgres@localhost:5433/postgres ; each test gets its own schema
    if pg:
        import psycopg
        schema = "t_" + uuid.uuid4().hex[:12]
        with psycopg.connect(pg, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA {schema}")
        monkeypatch.setenv("DATABASE_URL", pg + ("&" if "?" in pg else "?") + f"options=-csearch_path%3D{schema}")
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    for k, v in {
        "ANTHROPIC_API_KEY": "test", "ADMIN_PASSWORD": "", "GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csecret",
        "WA_VERIFY_TOKEN": "wa-verify", "WA_APP_SECRET": "wa-secret", "WA_ACCESS_TOKEN": "wa-token", "WA_PHONE_NUMBER_ID": "111",
        "IG_VERIFY_TOKEN": "ig-verify", "IG_APP_SECRET": "ig-secret", "IG_ACCESS_TOKEN": "ig-token", "IG_ACCOUNT_ID": "999",
        "GMAIL_POLLING": "0", "EMAIL_ALLOWED_SENDERS": "lead@test.example", "BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    config.reset_settings()
    fake = FakeLLM()
    llm.set_backend(fake)
    g, m = FakeGoogle(), FakeMeta()
    google.set_http_client(httpx.Client(transport=httpx.MockTransport(g.handler)))
    chbase.set_http(httpx.Client(transport=httpx.MockTransport(m.handler)))
    db.kv_set("google_refresh_token", "fake-refresh")
    db.kv_set("google_account_email", "assistant@test.example")

    class E:
        llm = fake
        google = g
        meta = m

    yield E
    llm.set_backend(None)
    google.set_http_client(None)
    chbase.set_http(None)
    config.reset_settings()
