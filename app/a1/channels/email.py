"""Email via the Gmail API of a dedicated test inbox (polled; see service.gmail_poll_loop)."""
from __future__ import annotations

import re
from email.utils import parseaddr

from .. import google
from .base import Inbound, SendResult


def is_automated(headers: dict[str, str], from_addr: str, own_addr: str) -> str | None:
    """Return a reason if this message must NOT trigger an automatic reply (loop and spam guard)."""
    if from_addr == own_addr:
        return "sent by ourselves"
    auto = headers.get("auto-submitted", "no").lower()
    if auto != "no":
        return f"Auto-Submitted: {auto}"
    if headers.get("precedence", "").lower() in {"bulk", "list", "junk"}:
        return "bulk/list precedence"
    if "list-unsubscribe" in headers or "list-id" in headers:
        return "mailing list"
    if re.search(r"(no-?reply|mailer-daemon|postmaster|notifications?@)", from_addr):
        return "no-reply sender"
    return None


def to_inbound(msg: dict, own_addr: str) -> tuple[Inbound | None, str | None]:
    h = msg["headers"]
    name, addr = parseaddr(h.get("from", ""))
    addr = addr.lower()
    if not addr:
        return None, "no sender address"
    reason = is_automated(h, addr, own_addr)
    if reason:
        return None, reason
    subject = h.get("subject", "(no subject)")
    text = f"Subject: {subject}\n\n{msg['text']}".strip()
    return Inbound("email", addr, text, msg["id"], name or None,
                   thread_ref={"thread_id": msg["thread_id"], "message_id": h.get("message-id"), "references": h.get("references"), "subject": subject}), None


def send(to: str, text: str, thread_ref: dict) -> SendResult:
    subject = thread_ref.get("subject") or "Your enquiry"
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    try:
        mid = google.gmail_send(to, subject, text, thread_ref.get("thread_id"), thread_ref.get("message_id"), thread_ref.get("references"))
        return SendResult(True, provider_id=mid)
    except google.AuthExpired as e:
        return SendResult(False, error=str(e), expired=True)
    except google.Uncertain as e:
        return SendResult(False, error=str(e), uncertain=True)
    except google.ApiError as e:
        return SendResult(False, error=str(e))
