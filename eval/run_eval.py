"""Run every scenario against fresh mocks and report.

    python -m eval.run_eval                # mock LLM (tests rules/plumbing)
    LLM_PROVIDER=groq python -m eval.run_eval   # real model (tests judgement)

Writes eval/results.md — the table for the reliability brief — and one trace per scenario in ./traces.
"""
from __future__ import annotations

import os
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
import sys
from pathlib import Path

os.environ.setdefault("AGENT_TODAY", "2026-09-13")

import yaml  # noqa: E402

from agent import pipeline  # noqa: E402
from agent.llm import get_llm  # noqa: E402
from agent.tools.mock import load_denial_message, mock_toolbox  # noqa: E402
from agent.tracing import TRACE_DIR, Tracer  # noqa: E402

HERE = Path(__file__).parent


def run_scenario(sc: dict, llm) -> tuple[bool, list[str], dict]:
    pipeline.PLANS.clear()
    tb, faults = mock_toolbox()
    for op, n in (sc.get("faults") or {}).items():
        faults.arm(op, n)
    tracer = Tracer(run_id=sc["id"], sink=TRACE_DIR / f"{sc['id']}.jsonl")
    if tracer.sink.exists():
        tracer.sink.unlink()

    plan = None
    for name in sc["denials"]:
        msg = load_denial_message(name)
        tb.mail.inbox.append(msg)
        plan = pipeline.handle_message(msg, tb, llm, tracer, auto_approve=not sc.get("reject"))
        if sc.get("reject") and plan and plan.status == "awaiting_approval":
            plan = pipeline.reject(plan.plan_id, tb, tracer, reason="eval")

    # ---- observed world ----
    obs = {
        "decision": plan.decision.type.value if plan else None,
        "plan_status": plan.status if plan else None,
        "needs_approval": plan.needs_approval if plan else None,
        "urgent": plan.decision.urgent if plan else None,
        "days_to_deadline": plan.decision.days_to_deadline if plan else None,
        "correction_new": plan.decision.correction.new if plan and plan.decision.correction else None,
        "emails_sent": len(tb.mail.sent),
        "email_to": tb.mail.sent[-1]["to"] if tb.mail.sent else None,
        "email_body": tb.mail.sent[-1]["body"] if tb.mail.sent else "",
        "email_attempts": next((a.attempts for a in plan.actions if a.type.value == "send_email"), None) if plan else None,
        "calendar_events": len(tb.calendar.events),
        "docs_saved": len(tb.docs.written),
        "inbox_unread": len(tb.mail.list_unread()),
        "ledger_updates": len(tb.ledger.updates),
        "ledger_status": (tb.ledger.get(plan.denial.claim_id).status if plan and tb.ledger.get(plan.denial.claim_id) else None),
        "chat_text": "\n".join(m["text"] for m in tb.chat.messages),
    }

    # ---- compare ----
    failures = []
    for k, want in sc["expect"].items():
        if k == "chat_contains":
            ok = want in obs["chat_text"]
            got = "…" + obs["chat_text"][-80:] if not ok else want
        elif k == "email_contains":
            ok = want in obs["email_body"]
            got = "(absent)" if not ok else want
        else:
            got = obs.get(k)
            ok = got == want
        if not ok:
            failures.append(f"{k}: expected {want!r}, got {got!r}")
    return not failures, failures, obs


def main() -> int:
    scenarios = yaml.safe_load((HERE / "scenarios.yaml").read_text(encoding="utf-8"))
    only = sys.argv[1:] or None
    llm = get_llm()
    rows, passed = [], 0
    for sc in scenarios:
        if only and sc["id"] not in only:
            continue
        ok, fails, obs = run_scenario(sc, llm)
        passed += ok
        rows.append((sc, ok, fails, obs))
        mark = "PASS" if ok else "FAIL"
        print(f"{mark}  {sc['id']}  {sc['name']}")
        for f in fails:
            print(f"        - {f}")
    total = len(rows)
    print(f"\n{passed}/{total} passed  (llm={llm.name}, today={os.environ['AGENT_TODAY']})")

    md = [f"# Eval results — {passed}/{total} passed (llm={llm.name})", "",
          "| ID | Scenario | Decision | Emails | Docs | Cal | Ledger | Result |", "|---|---|---|---|---|---|---|---|"]
    for sc, ok, fails, obs in rows:
        md.append(f"| {sc['id']} | {sc['name']} | {obs['decision']} | {obs['emails_sent']} | {obs['docs_saved']} | {obs['calendar_events']} | {obs['ledger_status']} | "
                  f"{'✅' if ok else '❌ ' + '; '.join(fails)} |")
    md += ["", "Traces: one JSONL per scenario in `traces/<ID>.jsonl`."]
    (HERE / "results.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
