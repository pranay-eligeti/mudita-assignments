"""Fictional sample input for Assignment 2 (no real company or person)."""

SAMPLE_MEETING_DATE = "2026-11-16"  # a Monday

SAMPLE_TRANSCRIPT = """Northwind Outdoors: Spring Catalogue Launch Sync
Date: Monday 2026-11-16. Attendees: Priya (Marketing lead), Dan (Engineering), Maya (Customer Success), Tom (Operations)
[00:01] Priya: Goal today is to lock the plan for the new online catalogue launch. I want it live for customers on December 7th.
[00:03] Dan: The catalogue pages are mostly built. I can finish the product page templates by Friday. The checkout integration is the risky bit and nobody has picked it up yet.
[00:06] Maya: We have 1,200 customers on the waitlist. I think we should email them the day the catalogue goes live, and the email needs to say the free-shipping offer is available.
[00:08] Priya: Agreed, we will send a launch email to the waitlist. Maya, you own that email.
[00:10] Tom: Free shipping needs to be confirmed. Last time the warehouse could not support it past 500 orders a week.
[00:12] Priya: Budget wise, I have approved eight thousand dollars for paid social ads to support the launch.
[00:14] Dan: Honestly, I'd rather we launch on December 14th. December 7th leaves almost no time for testing.
[00:16] Priya: Hmm. Let's keep December 7th for now but we may revisit.
[00:18] Maya: Someone needs to update the returns policy page before launch, I'm not sure who.
[00:20] Tom: I can cover warehouse capacity planning, I will send numbers by Wednesday.
[00:22] Priya: Great. Dan, you will also handle the redirect of the old catalogue URLs. Let's wrap up.
"""

SAMPLE_RULES = {
    "roles": ["Legal", "Finance", "QA"],
    "rules": [
        {"id": "R1", "text": "Every task deadline must fall on a business day (Monday to Friday) and not before the meeting date.",
         "check": {"type": "business_day"}},
        {"id": "R2", "text": "No single person may own more than 3 tasks.",
         "check": {"type": "max_tasks_per_owner", "max": 3}},
        {"id": "R3", "text": "Any task that launches, releases or publishes to customers must depend on a QA/testing task.",
         "check": {"type": "dependency", "if_title_matches": "^(launch|release|publish|send|go[- ]live)\\b",
                   "must_depend_on_matches": "qa|test"}},
        {"id": "R4", "text": "Tasks may only be owned by people named in the transcript or by the roles Legal, Finance or QA. Never invent an owner."},
        {"id": "R5", "text": "Any customer-facing message that makes a pricing, shipping or offer claim requires Legal review before it is sent."},
        {"id": "R6", "text": "Any spend above 5,000 USD requires Finance approval before the money is committed."},
    ],
}
