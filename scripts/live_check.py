"""One real LLM round trip per agent schema. Run: GEMINI_API_KEY=... MODEL=<id> python scripts/live_check.py
Uses SQLite in a temp file. Prints validity, tokens, latency. Costs nothing on the free tier but counts against its rate limit."""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "live.db"))
from app import llm
from app.a2 import agents
from app.a2.sample import SAMPLE_MEETING_DATE, SAMPLE_TRANSCRIPT
from app.a2.schemas import IntakeInput
from app.db import q1

t0 = time.time()
try:
    out = agents.run_intake("live", IntakeInput(transcript=SAMPLE_TRANSCRIPT, meeting_date=SAMPLE_MEETING_DATE))
except llm.LLMError as e:
    print("FAIL:", e); sys.exit(1)
r = q1("SELECT input_tokens i, output_tokens o FROM llm_calls ORDER BY id DESC LIMIT 1")
print(f"OK intake: {len(out.facts)} facts, tokens in/out {r['i']}/{r['o']}, {time.time()-t0:.1f}s")

if "--full" in sys.argv:  # whole Intake -> Planner -> Review pipeline on the sample transcript, real model
    from app.a2 import orchestrator as orch
    from app.a2.schemas import RulesDoc
    from app.a2.sample import SAMPLE_RULES
    sid = orch.ensure_session(None)
    rid = orch.create_run(sid, SAMPLE_TRANSCRIPT, SAMPLE_MEETING_DATE, RulesDoc.model_validate(SAMPLE_RULES), {})
    t1 = time.time()
    orch.execute_run(rid)
    v = orch.run_view(rid, sid)
    print(f"FULL RUN: status={v['status']} in {time.time()-t1:.1f}s, error={v.get('error')}")
    for st in v["steps"]:
        print(f"  {st['agent']:8s} round={st['round']} status={st['status']} err={(st.get('error') or '')[:100]}")
    print("  usage:", v.get("usage"))
    for h in v["handoffs"]:
        print(f"  handoff {h['from']}->{h['to']} {h['kind']} valid={h['valid']}")
    f = v.get("final") or {}
    print("  final keys:", sorted(f.keys()))
    print(f"  tasks={len((f.get('plan') or {}).get('tasks', []))} unresolved_issues={len(f.get('unresolved_issues', []))}")
