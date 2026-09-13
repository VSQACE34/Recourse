"""Turn a Decision into a Plan: an ordered list of idempotent actions across the apps.

Ordering rule: save the letter to Drive (our own record, harmless to redo), then external
side-effects (email), then calendar, then the ledger, then chat. The ledger is the source of truth, so it must never claim something was sent that
wasn't — it is written last, and only if everything before it succeeded.
"""
from __future__ import annotations

import uuid

from . import config
from .drafting import appeal_letter, escalation_text, plan_summary, resubmission_cover
from .models import Action, ActionType, Decision, DecisionType, LedgerRow, Plan
from .triage import Context


def _ledger_update(row: LedgerRow, d, dec: Decision, status: str, last_action: str) -> LedgerRow:
    r = row.model_copy()
    r.status = status
    r.decision = dec.type.value
    r.last_action = f"{config.today().isoformat()}: {last_action}"
    ids = [x for x in r.denial_ids.split(";") if x]
    if d.denial_id not in ids:
        ids.append(d.denial_id)
    r.denial_ids = ";".join(ids)
    if dec.appeal_deadline and dec.type in (DecisionType.APPEAL, DecisionType.ESCALATE, DecisionType.ESCALATE_PHYSICIAN):
        r.next_deadline = dec.appeal_deadline.isoformat()
    return r


def build_plan(ctx: Context, dec: Decision) -> Plan:
    d, c, row, payer = ctx.denial, ctx.claim, ctx.ledger_row, ctx.payer
    key = lambda t: f"{d.denial_id}:{t}"  # noqa: E731
    plan = Plan(plan_id=uuid.uuid4().hex[:8], denial=d, claim=c, decision=dec, summary=plan_summary(d, dec))
    acts: list[Action] = []

    if dec.type == DecisionType.NOOP:
        plan.status = "noop"
        return plan

    doc_path = lambda kind: f"appeals/{d.claim_id}_{d.denial_id}_{kind}.txt"  # noqa: E731

    if dec.type == DecisionType.CORRECT_RESUBMIT:
        plan.draft_text = resubmission_cover(d, c, dec)
        acts.append(Action(type=ActionType.SAVE_DOCUMENT, idempotency_key=key("doc"),
                           payload={"path": doc_path("resubmission"), "content": plan.draft_text}))
        acts.append(Action(type=ActionType.SEND_EMAIL, idempotency_key=key("email"), irreversible=True,
                           payload={"to": payer.claims_email, "subject": f"Corrected claim {d.claim_id} — {c.patient_name} — DOS {c.dos}", "body": plan.draft_text}))
        acts.append(Action(type=ActionType.UPDATE_LEDGER, idempotency_key=key("ledger"),
                           payload=_ledger_update(row, d, dec, "resubmitted", f"corrected {dec.correction.field}, resubmitted to {payer.claims_email}").model_dump()))

    elif dec.type == DecisionType.APPEAL:
        plan.draft_text = appeal_letter(d, c, dec, payer)
        acts.append(Action(type=ActionType.SAVE_DOCUMENT, idempotency_key=key("doc"),
                           payload={"path": doc_path("appeal"), "content": plan.draft_text}))
        acts.append(Action(type=ActionType.SEND_EMAIL, idempotency_key=key("email"), irreversible=True,
                           payload={"to": payer.appeals_email, "subject": f"Appeal — Claim {d.claim_id} — Control # {d.denial_id}", "body": plan.draft_text}))
        if dec.appeal_deadline:
            acts.append(Action(type=ActionType.CREATE_CALENDAR_EVENT, idempotency_key=key("calendar"),
                               payload={"title": f"Appeal deadline {d.claim_id} ({payer.name})", "day": dec.appeal_deadline.isoformat(),
                                        "description": f"{c.patient_name} · {d.carc} · ${d.denied_amount:,.2f} · appeal sent to {payer.appeals_email}"}))
        acts.append(Action(type=ActionType.UPDATE_LEDGER, idempotency_key=key("ledger"),
                           payload=_ledger_update(row, d, dec, "appealed", f"appeal sent to {payer.appeals_email}").model_dump()))

    elif dec.type == DecisionType.WRITE_OFF:
        # Financially material and hard to undo -> gated behind approval even though it's "just" a sheet update.
        acts.append(Action(type=ActionType.UPDATE_LEDGER, idempotency_key=key("ledger"), irreversible=True,
                           payload=_ledger_update(row, d, dec, "written_off", dec.reason).model_dump()))

    elif dec.type == DecisionType.CLOSE_DUPLICATE:
        acts.append(Action(type=ActionType.UPDATE_LEDGER, idempotency_key=key("ledger"),
                           payload=_ledger_update(row, d, dec, row.status, f"duplicate denial {d.denial_id} closed, no action").model_dump()))

    elif dec.type == DecisionType.PATIENT_RESPONSIBILITY:
        acts.append(Action(type=ActionType.UPDATE_LEDGER, idempotency_key=key("ledger"),
                           payload=_ledger_update(row, d, dec, "patient_balance", f"${d.denied_amount:.2f} {d.carc} to patient statement").model_dump()))

    elif dec.type in (DecisionType.ESCALATE, DecisionType.ESCALATE_PHYSICIAN):
        plan.draft_text = escalation_text(d, c, dec)
        if dec.appeal_deadline and c is not None:
            acts.append(Action(type=ActionType.CREATE_CALENDAR_EVENT, idempotency_key=key("calendar"),
                               payload={"title": f"Appeal deadline {d.claim_id} — UNRESOLVED", "day": dec.appeal_deadline.isoformat(),
                                        "description": plan.draft_text}))
        if row is not None:
            acts.append(Action(type=ActionType.UPDATE_LEDGER, idempotency_key=key("ledger"),
                               payload=_ledger_update(row, d, dec, "needs_review", dec.reason).model_dump()))

    # Every plan ends with a chat post so the team sees what happened (or didn't).
    acts.append(Action(type=ActionType.POST_CHAT, idempotency_key=key("chat"),
                       payload={"channel": config.BILLING_CHANNEL, "text": (plan.draft_text or "") if dec.type in (DecisionType.ESCALATE, DecisionType.ESCALATE_PHYSICIAN) else plan.summary}))

    plan.actions = acts
    plan.status = "awaiting_approval" if plan.needs_approval else "approved"
    return plan
