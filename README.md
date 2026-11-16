# Mudita AI Generalist assignments

One FastAPI application, two routes:

| Route | Assignment | What it is |
|---|---|---|
| `/a1` | 01 | Sales assistant for a fictional analytics consultancy (Lumen Analytics): Instagram + WhatsApp + email inquiries → LLM understanding → deterministic qualification and routing → Google Calendar booking, with human takeover |
| `/a2` | 02 | Operations coordinator: three separate LLM agents (Intake → Planning ⇄ Review) turning a fictional meeting transcript + company rules into a reviewed action plan |

> **Status honesty.** Labels used below: *implemented* = code exists; *tested* = covered by the automated suite against fakes; *verified live* = exercised against the real provider. Everything is **implemented + tested**; the *Verified live* column in [Status](#status) records what has actually been exercised against the real provider on the deployed site. Anything not marked "yes" has not been verified.

## Demo access

Fill in at submission time:

- App: `https://<your-deployment>` (`/a1`, `/a2`)
- `/a1` dashboard login: user `demo`, password `<ADMIN_PASSWORD>` (HTTP Basic). `/a2` needs no login; each browser session is private.
- Test inboxes to message (authorised test accounts only): Instagram `@<test-handle>`, WhatsApp `+<test-number>`, email `<test-inbox>`. See [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) for exactly what to send.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in; never commit .env
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --port 8000 --workers 1
pytest                          # 50 tests, no network or API keys needed
```

**Run with exactly one worker.** State lives in the database (Postgres in production via `DATABASE_URL`, SQLite for local runs and tests; one schema definition drives both); per-lead and per-run locks and the Gmail poller are in-process. Scaling out needs a queue and distributed locks (see Limitations).

Deploy: `Dockerfile` + `render.yaml` target **Render's free web service + free Postgres** (no card for Render itself). Free web services sleep after 15 minutes without traffic and wake in ~1 minute, which would drop a webhook that arrives while asleep, so see *Keeping it awake* below. Free Postgres expires 30 days after creation. Any host with one always-on process, Postgres and HTTPS also works.

### Keeping it awake
Point a free monitor (UptimeRobot or cron-job.org, every 5 minutes) at `https://<host>/healthz`. `.github/workflows/keepalive.yml` is a secondary pinger (set repo variable `APP_URL`); GitHub cron is best-effort and can lag, so it is not sufficient alone. Render may still restart a free instance at any time: webhook de-duplication and A2 restart recovery are built for that.

**Calendars:** the bundled policy points both reps at `calendar_id: "primary"` so it works with one test calendar (a booking for either rep then blocks both). For independent availability create two calendars in the test account and put their ids into the policy editor.

### Provider setup (summary of what I had to research)

| Channel | Mechanism | Needs | Known blockers |
|---|---|---|---|
| WhatsApp | Meta **WhatsApp Cloud API**, webhook `POST /webhooks/whatsapp` (HMAC `X-Hub-Signature-256`), send via `graph.facebook.com/{v}/{phone_number_id}/messages` | Meta app, test phone number, your own number added as an allowed recipient, subscribe the `messages` field | Test-mode access tokens last ~24h unless you create a System User token → shows as `expired` in the dashboard. Free-form replies only deliver inside the 24h customer-service window (our replies are always responses to an inbound message). Recipient not on the allow-list → error 131030 |
| Instagram | **Instagram API with Instagram Login** (`graph.instagram.com`), webhook `POST /webhooks/instagram`, send via `/{IG_ID}/messages` with a Bearer user token | Professional IG account, Meta app with `instagram_business_basic` + `instagram_business_manage_messages`, the messaging account added as an **Instagram Tester** (accepted inside Instagram settings), `messages` webhook field subscribed | Meta's docs say the app must be **published (Live)** to receive messaging webhooks, and several developers report DM webhooks not firing in development mode even with the tester role. If this blocks, the dashboard shows the exact state and Instagram must be marked **incomplete**, not working |
| Email | **Gmail API** on a dedicated test inbox, polled every 30s (no public endpoint or Pub/Sub needed), replies threaded with `In-Reply-To`/`References` | Google Cloud project, OAuth client (web), redirect URI `https://<host>/a1/auth/google/callback`, **Connect Google** button in the dashboard | OAuth consent screen in *Testing* mode expires refresh tokens after 7 days; for the 7-day review window publish the app to *In production* (unverified is fine for a test account) or the connection will show `expired` |
| Calendar | **Google Calendar API** (`freeBusy`, `events.insert/get`) via the same OAuth connection | same | same |

Why polling for email: one fewer moving part than Gmail push (Pub/Sub topic, IAM, watch renewal every 7 days); latency is irrelevant for this prototype. Why OAuth rather than a service account for Calendar: it works with a plain Google account without sharing calendars, and it exercises the real "expired connection" failure mode the brief asks to handle visibly.

## Architecture

```
            Instagram / WhatsApp (Meta webhooks, HMAC-verified)        Gmail (polled)
                         │                                                  │
                         └───────────────►  handle_inbound  ◄───────────────┘
                                   dedup(provider msg id) → lead (channel, sender_id) → store message
                                              │  takeover? → stop (store only)
                                              ▼
                       LLM  extract(): facts + exact quotes + intent   ── facts must be quotable from the LEAD's own words (verified in code)
                                              ▼
              engine.classify()  →  qualified | not_qualified | needs_more_info      (pure Python over editable policy)
              engine.route()     →  explicit rules, then least-loaded                (pure Python)
                                              ▼
        needs_more_info → LLM compose (missing fields only; template fallback)   not_qualified → template
        qualified → booking.available_slots() (Google freeBusy + policy hours) → offer 3 numbered slots
                       lead picks "2" → pending slot stored in DB → lead says "YES" → booking.book()
                                              ▼
              book(): deterministic event id → INSERT OR IGNORE (UNIQUE) → re-read freeBusy → single events.insert
                      409 → GET & verify · timeout/5xx → GET & verify (never blind retry) · 401 → visible 'expired'
```

Principle: **the model understands language; code owns policy.** The model can only *extract* statements a person made. It cannot set classification, assignment, availability, or trigger a booking, so "ignore your rules" has nothing to act on.

### Assignment 1 – how each requirement is met

| Requirement | Implementation | Test |
|---|---|---|
| Three real channels, one interface | `channels/{whatsapp,instagram,email}.py`, dashboard lists every lead with channel, sender, history, per-channel health | `test_whatsapp_inquiry_to_confirmed_booking`, `test_email_*`, `test_identity_is_not_merged_across_channels` |
| Context across messages and refreshes | Everything persisted in the database; the model receives the full stored conversation each turn; identity key is `(channel, provider sender id)`, never merged across channels | same |
| Qualified / not qualified / needs info + evidence | `engine.classify`; each extracted value stored with its quote and message index; rule trace shown | `test_classification_precedence_and_routing`, `test_whatsapp_inquiry_*` |
| Explicit routing | Ordered rules in the editable policy (`RT1..`), then least-loaded fallback; the trace shows which rule matched | same |
| Model can't invent policy/staff | Policy is data validated by pydantic; reps and services exist only in it; replies are template or constrained-compose with fallback | `test_instruction_to_ignore_rules_does_not_override_policy`, `test_model_cannot_invent_facts_without_a_real_quote`, `test_assistant_text_is_not_valid_evidence` |
| Real calendar | Google Calendar freeBusy + events | `test_whatsapp_inquiry_to_confirmed_booking` (fake Google at the HTTP layer) |
| Recheck before booking; no duplicates | Re-read freeBusy right before insert; deterministic event id + DB `UNIQUE(event_id)` + Calendar's 409 | `test_slot_taken_*`, `test_repeated_confirmation_never_duplicates`, `test_concurrent_confirmations_create_one_event` |
| Visible failures, no blind retry | Uncertain writes → `GET` by id to learn the truth; if that fails too the booking stays `uncertain`, blocks further automation and shows a *Reconcile* button | `test_uncertain_booking_*`, `test_timeout_where_event_was_not_created_*`, `test_unverifiable_outcome_*`, `test_expired_google_connection_*` |
| Human control | Lead status, assignment, meeting, action log; **Take over** re-checked before every automatic send and booking, also mid-processing | `test_takeover_*` |
| Safety extras | HMAC verification, webhook dedup, automated-mail ignore, sender allow-list for email, 12 bot msgs/hour loop guard | `test_webhook_*`, `test_loop_guard_*`, `test_email_threaded_reply_and_ignore_rules` |

### Assignment 2 – three agents, shared context

- **Intake Agent** → `IntakeOutput`: decisions/requirements/constraints, each with line numbers and an **exact quote** (verified against the transcript in code; unverifiable quotes become an `ungrounded` gap), `owner_stated`/`deadline_stated` only when explicitly in the text, plus `missing`/`conflict` gaps.
- **Planning Agent** ← `PlannerInput` (versioned context + rules + optional reviewer feedback + previous plan) → `PlanOutput`: tasks with `basis = supported | recommendation`, fact/rule references, owners (only participants or rule roles), deadlines (only if derivable), dependencies, `unresolved_questions`.
- **Review Agent** ← `ReviewInput` (the **original transcript**, context, rules, plan, deterministic validator findings) → `ReviewOutput`: verdict + specific issues with required corrections. Validator findings (cycles, unknown owners, invented deadlines, weekend deadlines, max-tasks-per-owner, QA dependency) are merged in code, so a lenient model cannot approve a rule violation (`test_injected_violation_is_sent_back_and_fixed`).
- **Orchestrator** (`a2/orchestrator.py`): persists run id, context versions, every step's input/output (`a2_steps`), every handoff (`a2_handoffs`, validated by constructing the receiver's input model *before* any model call), events. A step's identity is `hash(prompt version, model, agent, round, full input)`: same input → stored output reused (resume never repeats finished work); changed fact → different hash downstream → no stale reuse. Review ⇄ Planner loop is capped (`A2_MAX_REVIEW_ROUNDS`, default 3) then escalates to a human with the unresolved issues.
- **Correct a fact** in the UI → new context version → only Planner and Review re-run; Intake is reused because its input (the transcript) didn't change.
- **Session isolation**: cookie-bound session id; every query is scoped by it; another session's run id returns 404 for reads *and* writes; each agent call is stateless and built only from that run's own data (`test_sessions_are_isolated_in_storage_and_in_model_context`).
- **Simulated faults** are opt-in checkboxes, labelled `SIMULATED FAULT` in the step, the event log and the UI: (1) a model outage injected at a chosen agent, (2) a weekend deadline injected into the Planner's first plan. Real model failures and schema violations are handled by the same paths (`LLMError`, one bounded repair attempt, visible failure, resumable).

## Environment variables

See `.env.example`. Names only: `BASE_URL SECRET_KEY ADMIN_PASSWORD DATABASE_URL DB_PATH LLM_PROVIDER GEMINI_API_KEY ANTHROPIC_API_KEY LLM_MAX_ATTEMPTS MODEL PRICE_IN_PER_MTOK PRICE_OUT_PER_MTOK GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET GMAIL_POLLING GMAIL_POLL_SECONDS EMAIL_ALLOWED_SENDERS WA_VERIFY_TOKEN WA_APP_SECRET WA_ACCESS_TOKEN WA_PHONE_NUMBER_ID WA_GRAPH_VERSION TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN TWILIO_WHATSAPP_FROM IG_VERIFY_TOKEN IG_APP_SECRET IG_ACCESS_TOKEN IG_ACCOUNT_ID IG_GRAPH_VERSION MODEL_FALLBACKS A2_MAX_REVIEW_ROUNDS A2_FAULT_INJECTION`. All keys are server-side only; the browser never receives a token.

## Tests

`pytest` → 58 tests, run on SQLite by default and on Postgres 16 with `TEST_PG_URL=postgresql://user@host/db pytest` (each test gets its own schema). The LLM, Google (Calendar, Gmail, OAuth token endpoint) and Meta Graph API are replaced by scripted fakes at the **HTTP/SDK boundary**; everything in between (webhooks, signature checks, dedup, state machine, booking logic, SQL, orchestration) is production code. `tests/test_llm.py` verifies request shape, response parsing, error mapping, cost logging and (Gemini) 429/5xx backoff with `Retry-After` against mock transports, for both the Gemini REST backend and the Anthropic SDK backend. I also temporarily removed three safeguards (availability recheck, takeover check, deterministic event id) and confirmed the relevant tests fail.

What the tests **cannot** show: that the real model (Gemini free tier) extracts well from messy human text, that Meta delivers webhooks to a dev-mode app, or that Google's real latency/error behaviour matches the fakes. Those are covered by the live demo.

## Approximate model cost per run

Model: Gemini free tier (`LLM_PROVIDER=gemini`, model set by `MODEL`), so **$0 monetary cost per run, but rate limited**; 429s are retried with backoff. The dollar figures below are what the same token volume would cost on a paid model and use assumed prices. Token counts measured **in the UI** per run/lead from the API's reported token usage (`llm_calls` table). **Measured on the deployed site (Gemini free tier, token counts as reported by the API):** one A2 run took 5 LLM calls and about 15 to 16k input plus about 4k output tokens when the Review loop used one revision round (16,197 + 3,961 and 14,790 + 3,858 in two runs); a run that was also corrected twice (three context versions) took 11 calls and 35,476 + 7,898 tokens. Each extra Planner + Review round adds roughly 2 calls and 5k tokens. A1 is cheaper: across 11 calls so far the app used 5,562 input and 1,509 output tokens (about 500 in and 140 out per call), and a full inquiry to booking conversation is about 3 to 5 calls. At the assumed paid-model prices below that is about $0.10 per A2 run (about $0.02 per extra round) and about $0.01 to $0.02 per A1 conversation; on the free tier the cost is $0. Slot choice and YES replies are parsed deterministically and cost nothing. My earlier pre-deployment estimate of about $0.03 per A2 run was too low by roughly 3x because it underestimated prompt size. Slot choice and "YES" replies are parsed deterministically and cost nothing. Prices are configurable and the defaults ($3 / $15 per M tokens) are **assumptions I have not verified**; replace them with current pricing before quoting a dollar figure.

## Assignment 1: cases verified live (email + Google Calendar, real Gemini)

Run on the deployed site on 16 Nov 2026 with the assistant mailbox connected through OAuth and a separate sender address:

| Case | What was observed |
|---|---|
| Full booking | Real inquiry, grounded extraction, qualification, routing to a rep, three free slots from real freebusy, confirmation by email, one real Google Calendar event |
| Repeated confirmation | A second YES produced exactly one event |
| Incomplete inquiry plus "ignore your rules" injection | Override refused with a notice; lead stayed needs_more_info; only stated facts extracted; no booking; missing service, budget and timeline asked for |
| Human takeover | While in control, the lead's reply was recorded but no bot reply, extraction or booking happened; after Resume and Retry the lead was qualified, routed and offered slots |
| Occupied slot | A conflicting calendar event was created on the chosen slot; the recheck at confirmation found it busy, logged slot_taken, created no event and offered new times |

The Gmail channel is polled every 30 seconds, so replies can lag by up to that interval. Not verified live: WhatsApp replies, Instagram (see below), and an expired-Google-connection state (covered by tests only).

## WhatsApp: receive-only on a Twilio trial. Instagram: blocked.

**WhatsApp, partial (checked 16 Nov 2026).** Because the Meta route was blocked (below), the WhatsApp channel also supports a Twilio WhatsApp sandbox: set the `TWILIO_*` variables and the signature-checked webhook `POST /webhooks/twilio/whatsapp` is used. Live result: a real message from the verified tester number was received, classified with evidence, routed and given slot offers, and the lead and messages show in the dashboard. **Replies do not reach WhatsApp.** The app returns them in the webhook response and logs them as sent, but the Twilio trial sender only allows template messages: a free-text REST send fails with error 21654 ("ContentSid Required"), and nothing arrived on the phone. I could not see Twilio's per-message status, because its debugger needs a paid plan, so the exact cause is not confirmed. A trial also lets only the verified tester message the number, and the sandbox session expires after three days, so outside testers cannot use it. No end-to-end WhatsApp booking is claimed.

**Meta route (WhatsApp Cloud API and Instagram).** Both are implemented (webhook verification, signature check, dedup, send) and covered by tests against fakes, and the webhook endpoints are deployed, but **neither has been verified against the real provider**. In the Meta for Developers app-creation wizard (use cases WhatsApp and Instagram), the Business step reported "No businesses available", and Next stayed disabled because a business portfolio is required. Creating one failed with this message from Meta on my account: "Your Advertising Access is Restricted. You're no longer allowed to use Meta technologies to advertise. You can't run ads, manage advertising assets, or create new ad accounts or business portfolios." I did not try to work around the restriction. Until a Meta app exists under an account that can hold a business portfolio, the Meta WhatsApp route and Instagram stay incomplete, and no Instagram demo case is claimed.

## Known limitations

- A2 corrected facts: after a user correction, the superseded wording and its quote are hidden from the Planner and Review models (kept in storage for audit); the UI still shows the original transcript quote.
- A2 fact grounding: the code verifies that each fact's quoted text appears in the transcript, not that the quote supports every word of the fact; a fact can carry wording its quote does not cover.
- A2 rule coverage: R1 to R4 are checked in code; R5 (Legal review) and R6 (Finance approval) are checked only by the Review model, so a plan can miss them.
- A2 output varies between runs of the same input; the review loop bounds this but does not remove it.
- A1 numeric parsing accepts words and week units ("four weeks") but rejects ambiguous bases such as "$2,000 per month" and asks again. A later message replaces an earlier verified value only if it restates that field.
- Single process. Writers are serialised with a Postgres advisory lock; multi-instance additionally needs a job queue and per-lead distributed locks.
- Free-tier constraints: Gemini free-tier rate limits and data-use terms (only fictional data is sent); Render free sleep/restarts; free Postgres expires after 30 days.
- Budget parsing only accepts USD figures; other currencies are asked about again. Timeline is expressed in weeks.
- Slots are offered in the policy timezone only; the lead's timezone is not collected.
- One meeting per lead; rescheduling/cancellation is handled by a human.
- WhatsApp (Twilio trial): receive-only, see above. WhatsApp: only text messages are understood; replies outside the 24h window would need a template (not implemented). Instagram: text only; webhook delivery in development mode may be unreliable (see table).
- Gmail polling replies to the latest message of a thread and ignores quoted history heuristically (lines after `On … wrote:`/`>`).
- Title-regex rules in A2 (`R3`) are heuristics; semantic rule checks rely on the Review Agent, which can miss things. Deterministic checks only cover what is expressible as `business_day`, `max_tasks_per_owner`, `dependency` plus structural validation.
- A2 intake relies on exact-quote verification; a faithful paraphrase that is not a substring is flagged `ungrounded` (conservative by design).
- Policy and dashboard auth is a single shared HTTP Basic password, adequate for a demo, not production.
- Token storage: the Google refresh token is stored in the database unencrypted; use a secrets manager/column encryption in production.

## Tools used (disclosure)

Built with Claude Code (Claude Sonnet 5.5) as a coding assistant for scaffolding, implementation and tests; I reviewed the design decisions and test results. The application itself calls Google's Gemini API (free tier); an Anthropic backend is also implemented and selectable with `LLM_PROVIDER=anthropic`.

## Status

| Piece | Implemented | Tested (fakes) | Verified live |
|---|---|---|---|
| A2 three-agent pipeline, review loop, fact correction + rerun, resume, simulated faults | yes | yes | yes: real Gemini on the deployed site (weekend-deadline fault caught by Review; planner outage then Resume reused Intake; fact correction rerun dropped the Finance task at 3,000 USD) |
| A1 policy engine, extraction guardrails, state machine | yes | yes | yes: real model on the email channel |
| Email (Gmail) inbound/outbound | yes | yes | yes: real inquiry, qualification, slot offer, confirmation |
| Google Calendar availability + booking | yes | yes | yes: real event created; repeated confirmation produced exactly one event |
| WhatsApp inbound/outbound | yes | yes | **PARTIAL:** inbound verified live over a Twilio trial; replies not delivered; Meta route blocked (see below) |
| Instagram messaging inbound/outbound | yes | yes | **INCOMPLETE, blocked by Meta** (see below) |
| Deployment + 7-day uptime | yes (Render free) | n/a | deployed; uptime monitor running, 7 days not yet elapsed |
