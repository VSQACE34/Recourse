"""Execute a plan against the toolbox.

Guarantees:
- Nothing irreversible runs until plan.status == "approved".
- Each action is retried once. On a second failure the remaining actions are *skipped*
  (never executed out of order), the plan is marked failed, and the team is told in chat.
- Because the ledger write is last, a failed plan leaves no trace on the ledger, so the same
  denial can be re-ingested and retried; the mail/calendar idempotency keys make that safe.
"""
from __future__ import annotations

from . import config
from .models import Action, ActionType, LedgerRow, Plan
from .tools.base import Toolbox
from .tracing import Tracer

MAX_ATTEMPTS = 2


def approval_blocks(plan: Plan) -> list[dict]:
    """Slack Block Kit: summary + draft + approve/reject buttons."""
    draft = (plan.draft_text or "")[:2800]
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Approval needed*\n{plan.summary}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "Rules: " + " → ".join(plan.decision.rule_trace)[:600]}]},
    ]
    if draft:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{draft}```"}})
    blocks.append({"type": "actions", "block_id": f"plan:{plan.plan_id}", "elements": [
        {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Approve & execute"}, "action_id": "approve", "value": plan.plan_id},
        {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "Reject"}, "action_id": "reject", "value": plan.plan_id},
    ]})
    return blocks


def request_approval(plan: Plan, tb: Toolbox, tracer: Tracer) -> None:
    with tracer.span("chat.request_approval", plan_id=plan.plan_id):
        res = tb.chat.post(config.BILLING_CHANNEL, f"Approval needed: {plan.summary}", blocks=approval_blocks(plan),
                           idempotency_key=f"{plan.denial.denial_id}:approval")
        if isinstance(res, dict) and res.get("ts"):
            plan.approval_msg = {"channel": res.get("channel", config.BILLING_CHANNEL), "ts": res["ts"]}


def _run(a: Action, tb: Toolbox) -> dict:
    p = a.payload
    if a.type == ActionType.SAVE_DOCUMENT:
        return tb.docs.write_text(p["path"], p["content"], a.idempotency_key)
    if a.type == ActionType.SEND_EMAIL:
        return tb.mail.send(p["to"], p["subject"], p["body"], a.idempotency_key)
    if a.type == ActionType.CREATE_CALENDAR_EVENT:
        return tb.calendar.create_event(p["title"], p["day"], p["description"], a.idempotency_key)
    if a.type == ActionType.UPDATE_LEDGER:
        tb.ledger.update(LedgerRow(**p))
        return {"claim_id": p["claim_id"], "status": p["status"]}
    if a.type == ActionType.POST_CHAT:
        return tb.chat.post(p["channel"], p["text"], p.get("blocks"), a.idempotency_key)
    raise ValueError(f"unknown action {a.type}")


def execute(plan: Plan, tb: Toolbox, tracer: Tracer) -> Plan:
    if plan.status == "noop":
        return plan
    if plan.status != "approved":
        raise PermissionError(f"plan {plan.plan_id} is {plan.status}; refusing to execute")

    failed = False
    for a in plan.actions:
        if failed:
            a.status = "skipped"
            tracer.log("action.skipped", type=a.type.value, key=a.idempotency_key)
            continue
        while a.attempts < MAX_ATTEMPTS and a.status != "done":
            a.attempts += 1
            try:
                with tracer.span("action", type=a.type.value, key=a.idempotency_key, attempt=a.attempts):
                    a.result = _run(a, tb)
                    a.status = "done"
            except Exception as e:  # noqa: BLE001
                a.error = str(e)
                a.status = "failed"
        if a.status == "failed":
            failed = True

    plan.status = "failed" if failed else "executed"
    if failed:
        bad = next(x for x in plan.actions if x.status == "failed")
        try:
            tb.chat.post(config.BILLING_CHANNEL,
                         f"⚠️ Plan {plan.plan_id} for {plan.denial.claim_id} FAILED at {bad.type.value} after {bad.attempts} attempts: {bad.error}. "
                         f"Remaining steps skipped; ledger untouched. Re-ingest to retry.",
                         idempotency_key=f"{plan.denial.denial_id}:failure")
        except Exception as e:  # noqa: BLE001
            tracer.log("chat.failure_notice_failed", error=str(e))
    tracer.log("plan.finished", plan_id=plan.plan_id, status=plan.status,
               actions=[(x.type.value, x.status, x.attempts) for x in plan.actions])
    return plan
