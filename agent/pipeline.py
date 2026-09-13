"""Orchestration. One denial in, one plan out, executed after approval."""
from __future__ import annotations

import json
from typing import Optional

from . import config
from .denial_parser import ParseError, parse_denial
from .executor import execute, request_approval
from .llm import LLM
from .models import Claim, Denial, Patient, PayerRules, Plan
from .planner import build_plan
from .tools.base import Message, Toolbox
from .tracing import Tracer
from .triage import Context, triage

# In-memory plan registry (fine for a one-night demo; swap for the sheet/MongoDB if needed).
PLANS: dict[str, Plan] = {}


def gather_context(denial: Denial, tb: Toolbox, tracer: Tracer) -> Context:
    with tracer.span("context.gather", claim_id=denial.claim_id):
        claims = {c["claim_id"]: Claim(**c) for c in json.loads(tb.docs.read_text("claims/claims.json"))}
        patients = {p["patient_id"]: Patient(**p) for p in json.loads(tb.docs.read_text("claims/patients.json"))}
        payers = {k: PayerRules(name=k, **v) for k, v in json.loads(tb.docs.read_text("policies/payers.json")).items()}

        claim = claims.get(denial.claim_id)
        patient = patients.get(claim.patient_id) if claim else None
        row = tb.ledger.get(denial.claim_id)
        payer = payers.get(denial.payer) or (payers.get(claim.payer) if claim else None)

        note: Optional[str] = None
        if claim and claim.note_file and tb.docs.exists(claim.note_file):
            note = tb.docs.read_text(claim.note_file)

        policy: Optional[str] = None
        if payer:
            ppath = payer.policies.get(denial.carc_code)
            if ppath and tb.docs.exists(ppath):
                policy = tb.docs.read_text(ppath)

        tracer.log("context.result", claim_found=claim is not None, ledger_found=row is not None,
                   patient_found=patient is not None, payer=payer.name if payer else None,
                   note_found=note is not None, policy_found=policy is not None)
        return Context(denial=denial, claim=claim, ledger_row=row, patient=patient, payer=payer,
                       note=note, policy=policy, today=config.today())


def find_open_plan(denial_id: str) -> Optional[Plan]:
    """A plan that is pending or already done for this denial. Building a second one would let a
    human approve the same action twice, so ingest returns the existing plan instead."""
    for p in PLANS.values():
        if p.denial.denial_id == denial_id and p.status in ("awaiting_approval", "approved", "executed"):
            return p
    return None


def handle_denial(denial: Denial, tb: Toolbox, llm: LLM, tracer: Tracer, auto_approve: bool = False) -> Plan:
    existing = find_open_plan(denial.denial_id)
    if existing:
        tracer.log("plan.duplicate", denial_id=denial.denial_id, existing_plan=existing.plan_id, status=existing.status)
        return existing
    ctx = gather_context(denial, tb, tracer)
    decision = triage(ctx, llm, tracer)
    plan = build_plan(ctx, decision)
    PLANS[plan.plan_id] = plan
    tracer.log("plan.built", plan_id=plan.plan_id, decision=decision.type.value, needs_approval=plan.needs_approval,
               actions=[a.type.value for a in plan.actions])

    if plan.status == "awaiting_approval":
        if auto_approve:
            plan.status = "approved"
        else:
            request_approval(plan, tb, tracer)
            return plan
    return execute(plan, tb, tracer)


def handle_message(msg: Message, tb: Toolbox, llm: LLM, tracer: Tracer, auto_approve: bool = False) -> Optional[Plan]:
    tracer.log("message.received", id=msg.id, subject=msg.subject)
    try:
        denial = parse_denial(msg, llm=llm, tracer=tracer)
    except ParseError as e:
        tracer.log("message.unparseable", error=str(e))
        tb.chat.post(config.BILLING_CHANNEL, f"⚠️ Could not parse a denial from message '{msg.subject}': {e}. Left in inbox.",
                     idempotency_key=f"{msg.id}:unparseable")
        return None
    tracer.log("denial.parsed", denial=denial)
    plan = handle_denial(denial, tb, llm, tracer, auto_approve=auto_approve)
    tb.mail.mark_processed(msg.id)
    return plan


def approve(plan_id: str, tb: Toolbox, tracer: Tracer) -> Plan:
    plan = PLANS[plan_id]
    if plan.status != "awaiting_approval":
        raise ValueError(f"plan {plan_id} is {plan.status}")
    # Re-check the ledger at approval time: the world may have moved since the plan was built
    # (another plan for the same denial executed, or a human worked it by hand).
    row = tb.ledger.get(plan.denial.claim_id)
    if row and row.has_denial(plan.denial.denial_id):
        plan.status = "noop"
        tracer.log("plan.stale", plan_id=plan_id, reason="denial already recorded on ledger at approval time")
        return plan
    plan.status = "approved"
    tracer.log("plan.approved", plan_id=plan_id)
    return execute(plan, tb, tracer)


def reject(plan_id: str, tb: Toolbox, tracer: Tracer, reason: str = "") -> Plan:
    plan = PLANS[plan_id]
    plan.status = "rejected"
    tracer.log("plan.rejected", plan_id=plan_id, reason=reason)
    tb.chat.post(config.BILLING_CHANNEL, f"Plan {plan_id} for {plan.denial.claim_id} rejected. No actions taken.",
                 idempotency_key=f"{plan.denial.denial_id}:rejected")
    return plan


def poll_inbox(tb: Toolbox, llm: LLM, tracer: Tracer, auto_approve: bool = False) -> list[Plan]:
    out = []
    for msg in tb.mail.list_unread():
        p = handle_message(msg, tb, llm, tracer, auto_approve=auto_approve)
        if p:
            out.append(p)
    return out


APPROVE_EMOJI = {"white_check_mark", "heavy_check_mark", "+1"}
REJECT_EMOJI = {"x", "no_entry", "-1"}


def poll_reactions(tb: Toolbox, tracer: Tracer) -> list[dict]:
    """No-tunnel approval path: look at every plan awaiting approval, read the emoji reactions on
    its Slack card, and approve on ✅ / reject on ❌. Rejection wins if both are present."""
    out = []
    for plan in list(PLANS.values()):
        if plan.status != "awaiting_approval" or not plan.approval_msg:
            continue
        try:
            names = tb.chat.reactions(plan.approval_msg["channel"], plan.approval_msg["ts"])
        except Exception as e:  # noqa: BLE001
            tracer.log("chat.reactions_failed", plan_id=plan.plan_id, error=str(e))
            continue
        if names & REJECT_EMOJI:
            reject(plan.plan_id, tb, tracer, reason="rejected via ❌ reaction")
            _replace_card(tb, plan, f"⛔ Rejected via reaction — plan {plan.plan_id}, no actions taken")
            out.append({"plan_id": plan.plan_id, "status": plan.status, "via": "reaction"})
        elif names & APPROVE_EMOJI:
            approve(plan.plan_id, tb, tracer)
            _replace_card(tb, plan, f"✅ Approved via reaction — plan {plan.plan_id} {plan.status}")
            out.append({"plan_id": plan.plan_id, "status": plan.status, "via": "reaction"})
    return out


def _replace_card(tb: Toolbox, plan: Plan, text: str) -> None:
    try:
        tb.chat.update(plan.approval_msg["channel"], plan.approval_msg["ts"], text)
    except Exception:  # noqa: BLE001
        pass


def ledger_report(tb: Toolbox) -> dict:
    """What the agent has done, straight from the ledger (the source of truth, not our memory)."""
    from collections import Counter, defaultdict
    counts, dollars = Counter(), defaultdict(float)
    open_deadlines = []
    for r in tb.ledger.all():
        if not r.decision:
            continue
        counts[r.decision] += 1
        try:
            dollars[r.decision] += float(r.billed or 0)
        except ValueError:
            pass
        if r.next_deadline and r.status in ("appealed", "needs_review"):
            open_deadlines.append((r.next_deadline, r.claim_id, r.status))
    open_deadlines.sort()
    return {"by_decision": dict(counts), "dollars_by_decision": {k: round(v, 2) for k, v in dollars.items()},
            "open_deadlines": open_deadlines[:10], "as_of": config.today().isoformat()}


def post_report(tb: Toolbox, tracer: Tracer) -> dict:
    rep = ledger_report(tb)
    lines = [f"*Denial worklist — {rep['as_of']}*"]
    for k, n in sorted(rep["by_decision"].items()):
        lines.append(f"• {k.replace('_', ' ')}: {n} claim{'s' if n != 1 else ''} · ${rep['dollars_by_decision'].get(k, 0):,.2f} billed")
    if rep["open_deadlines"]:
        lines.append("Next deadlines: " + ", ".join(f"{c} {d} ({s})" for d, c, s in rep["open_deadlines"][:5]))
    text = "\n".join(lines)
    tb.chat.post(config.BILLING_CHANNEL, text, idempotency_key=f"report:{rep['as_of']}")
    tracer.log("report.posted", report=rep)
    return rep


def receipts(plan: Plan, tb: Toolbox) -> list[dict]:
    """Read-after-write evidence for an executed plan: go back to each app and fetch what we did."""
    out = []
    for a in plan.actions:
        if a.status != "done" or not a.result:
            continue
        r = a.result
        try:
            if a.type.value == "send_email":
                back = tb.mail.read_back(r["id"]) if hasattr(tb.mail, "read_back") else None
                out.append({"app": "Gmail", "what": f"Sent to {r.get('to')}", "detail": f"{r.get('subject')}",
                            "verified": bool(back), "when": (back or {}).get("date"), "link": (back or {}).get("link"),
                            "note": "fetched back from Gmail Sent by message id" if back else "could not read back"})
            elif a.type.value == "save_document":
                out.append({"app": "Drive", "what": "Letter saved", "detail": r.get("path") or a.payload.get("path"),
                            "verified": True, "link": r.get("link")})
            elif a.type.value == "create_calendar_event":
                out.append({"app": "Calendar", "what": "Deadline booked", "detail": a.payload.get("title"),
                            "verified": True, "link": r.get("htmlLink")})
            elif a.type.value == "update_ledger":
                link = tb.ledger.link(plan.denial.claim_id) if hasattr(tb.ledger, "link") else None
                row = tb.ledger.get(plan.denial.claim_id)
                out.append({"app": "Sheets", "what": f"Ledger row {plan.denial.claim_id}", "detail": f"status = {row.status if row else '?'}",
                            "verified": bool(row and row.has_denial(plan.denial.denial_id)), "link": link,
                            "note": "re-read from the sheet after writing"})
            elif a.type.value == "post_chat":
                link = tb.chat.permalink(r.get("channel"), r.get("ts")) if hasattr(tb.chat, "permalink") else None
                out.append({"app": "Slack", "what": "Posted to billing channel", "detail": a.payload.get("text", "")[:80],
                            "verified": True, "link": link})
        except Exception as e:  # noqa: BLE001
            out.append({"app": a.type.value, "what": "receipt lookup failed", "detail": str(e), "verified": False})
    return out