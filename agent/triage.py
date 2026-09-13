"""Decide what to do with a denial.

Shape: a deterministic rule cascade decides *which kind* of denial this is and what's
mechanically true (deadline, duplicate, proof of filing, economics). The LLM is called only
for the two things that need reading comprehension: does the note satisfy the policy, and
what is the concrete fix for a rejected claim. Every rule that fires is recorded in
Decision.rule_trace so the plan is explainable line by line.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from . import config
from .llm import LLM
from .models import Claim, Correction, Decision, DecisionType, Denial, LedgerRow, Patient, PayerRules
from .tracing import Tracer

CORRECTABLE = {"4", "16", "22", "140", "181", "182"}      # submission / coding errors
JUDGEMENT = {"50", "97", "151", "197", "56"}              # medical necessity / bundling / auth
PATIENT_RESP_GROUP = "PR"


@dataclass
class Context:
    denial: Denial
    claim: Optional[Claim]
    ledger_row: Optional[LedgerRow]
    patient: Optional[Patient]
    payer: Optional[PayerRules]
    note: Optional[str]
    policy: Optional[str]
    today: date


def _deadline(ctx: Context) -> tuple[Optional[date], Optional[int]]:
    if not ctx.payer or not ctx.denial.remittance_date:
        return None, None
    dl = ctx.denial.remittance_date + timedelta(days=ctx.payer.appeal_window_days_from_denial)
    return dl, (dl - ctx.today).days


def triage(ctx: Context, llm: LLM, tracer: Tracer) -> Decision:
    d = ctx.denial
    trace: list[str] = []
    deadline, days = _deadline(ctx)
    urgent = days is not None and 0 <= days <= config.URGENT_DAYS
    expired = days is not None and days < 0

    def done(t: DecisionType, reason: str, **kw) -> Decision:
        dec = Decision(type=t, reason=reason, urgent=urgent and t in (DecisionType.APPEAL, DecisionType.CORRECT_RESUBMIT, DecisionType.ESCALATE, DecisionType.ESCALATE_PHYSICIAN),
                       appeal_deadline=deadline, days_to_deadline=days, rule_trace=trace, **kw)
        tracer.log("triage.decision", decision=dec)
        return dec

    # 1. Do we even know this claim?
    if ctx.claim is None or ctx.ledger_row is None:
        trace.append("R1 claim not found on ledger/records")
        return done(DecisionType.ESCALATE, f"Claim {d.claim_id} is not on the ledger; cannot act safely.", confidence=1.0)

    # 2. Idempotency: have we already handled this exact denial?
    if ctx.ledger_row.has_denial(d.denial_id):
        trace.append(f"R2 denial {d.denial_id} already recorded on ledger")
        return done(DecisionType.NOOP, "Denial already processed; no action.", confidence=1.0)

    code, group = d.carc_code, d.carc_group

    # 3. Patient responsibility: nothing to appeal.
    if group == PATIENT_RESP_GROUP:
        trace.append(f"R3 CARC group PR ({d.carc}) = patient responsibility")
        return done(DecisionType.PATIENT_RESPONSIBILITY, f"{d.carc}: member cost-share (${d.denied_amount:.2f}). Bill patient; no appeal.", confidence=1.0)

    # 4. Contractual adjustment: write off by contract.
    if code == "45":
        trace.append("R4 CARC 45 contractual adjustment")
        return done(DecisionType.WRITE_OFF, "Charge exceeds contracted fee schedule; contractual write-off.", confidence=1.0)

    # 5. Duplicate: check our own ledger before believing the payer.
    if code == "18":
        if ctx.ledger_row.status.lower() == "paid":
            trace.append(f"R5 CARC 18 and ledger shows paid {ctx.ledger_row.paid_amount} on {ctx.ledger_row.paid_date}")
            return done(DecisionType.CLOSE_DUPLICATE, f"Duplicate of a claim already paid ${ctx.ledger_row.paid_amount} on {ctx.ledger_row.paid_date}. Close, no resubmission.", confidence=1.0)
        trace.append("R5 CARC 18 but ledger does NOT show this claim paid")
        return done(DecisionType.ESCALATE, "Payer says duplicate but our ledger shows no payment. Needs reconciliation before any action.", confidence=0.9)

    # 6. Timely filing: appeal only with proof.
    if code == "29":
        if ctx.payer and ctx.claim.first_submitted:
            window_end = ctx.claim.dos + timedelta(days=ctx.payer.timely_filing_days_from_dos)
            in_window = ctx.claim.first_submitted <= window_end
            has_proof = bool(ctx.claim.clearinghouse_ref)
            trace.append(f"R6 CARC 29: first_submitted={ctx.claim.first_submitted} window_end={window_end} in_window={in_window} proof={has_proof}")
            if in_window and has_proof and not expired:
                return done(DecisionType.APPEAL, f"Proof of timely filing exists: submitted {ctx.claim.first_submitted} (ref {ctx.claim.clearinghouse_ref}), within {ctx.payer.timely_filing_days_from_dos} days of DOS.", confidence=0.95)
        trace.append("R6 CARC 29 without proof of timely filing")
        return done(DecisionType.WRITE_OFF, "Filed late with no proof of timely filing; not appealable and not billable to patient.", confidence=0.95)

    # 7. Correctable submission errors.
    if code in CORRECTABLE:
        # 7a. Deterministic first: a member-ID mismatch needs no reading, just a comparison.
        if ctx.patient and d.member_id_submitted and d.member_id_submitted.strip() != ctx.patient.member_id.strip():
            trace.append(f"R7a member ID on denial ({d.member_id_submitted}) != patient record ({ctx.patient.member_id}); deterministic fix")
            corr = Correction(field="member_id", old=d.member_id_submitted, new=ctx.patient.member_id,
                              rationale="Submitted member ID does not match the ID on the patient record.")
            return done(DecisionType.CORRECT_RESUBMIT, f"Fix {corr.field}: {corr.old} -> {corr.new}. {corr.rationale}", confidence=1.0, correction=corr)
        # 7b. Otherwise the fix needs reading the note (laterality, diagnosis vs procedure): ask the model.
        trace.append(f"R7 CARC {code} correctable; asking for concrete fix")
        corr = llm.propose_correction(d, ctx.claim, ctx.patient, ctx.note or "", tracer)
        if corr:
            trace.append(f"R7 fix found: {corr.field} {corr.old} -> {corr.new}")
            return done(DecisionType.CORRECT_RESUBMIT, f"Fix {corr.field}: {corr.old} -> {corr.new}. {corr.rationale}", confidence=0.9, correction=corr)
        trace.append("R7 no determinable fix")
        return done(DecisionType.ESCALATE, "Submission error but the correction cannot be determined from the records on file.", confidence=0.8)

    # 8. Judgement calls: medical necessity, bundling, authorisation.
    if code in JUDGEMENT:
        if expired:
            trace.append(f"R8a appeal window closed on {deadline}")
            return done(DecisionType.WRITE_OFF, f"Appeal window closed on {deadline}; nothing to do but write off.", confidence=1.0)
        if d.denied_amount is not None and d.denied_amount < config.MIN_APPEAL_AMOUNT:
            trace.append(f"R8b denied ${d.denied_amount} < MIN_APPEAL_AMOUNT ${config.MIN_APPEAL_AMOUNT}")
            return done(DecisionType.WRITE_OFF, f"Denied amount ${d.denied_amount:.2f} is below the appeal threshold.", confidence=1.0)
        if not ctx.note:
            trace.append("R8c clinical note not found")
            return done(DecisionType.ESCALATE, "Clinical note for this DOS is missing from Drive; cannot assess without it.", confidence=1.0)
        if not ctx.policy:
            trace.append("R8d payer policy not on file")
            return done(DecisionType.ESCALATE, f"No {d.payer} policy on file for CARC {code}; cannot assess against criteria.", confidence=1.0)

        trace.append(f"R8 CARC {code}: assessing note against policy")
        a = llm.assess_documentation(d, ctx.claim, ctx.note, ctx.policy, tracer)
        if a.confidence < config.ESCALATE_CONFIDENCE:
            trace.append(f"R8e assessment confidence {a.confidence:.2f} too low")
            return done(DecisionType.ESCALATE, f"Could not confidently assess documentation ({a.rationale}).", confidence=a.confidence, assessment=a)
        if a.supported and a.confidence >= config.APPEAL_CONFIDENCE:
            trace.append(f"R8f supported (conf {a.confidence:.2f}); criteria met: {a.criteria_met}")
            return done(DecisionType.APPEAL, "Documentation meets policy criteria: " + "; ".join(a.criteria_met), confidence=a.confidence, assessment=a)
        if not a.supported and a.confidence >= config.APPEAL_CONFIDENCE:
            trace.append(f"R8g NOT supported (conf {a.confidence:.2f}); unmet: {a.criteria_unmet}")
            return done(DecisionType.ESCALATE_PHYSICIAN, "Documentation does not meet policy criteria (" + "; ".join(a.criteria_unmet) + "). Physician to confirm whether an addendum is warranted or to accept write-off.", confidence=a.confidence, assessment=a)
        trace.append(f"R8h ambiguous (supported={a.supported}, conf {a.confidence:.2f})")
        return done(DecisionType.ESCALATE, f"Assessment ambiguous: {a.rationale}", confidence=a.confidence, assessment=a)

    # 9. Anything else: don't guess.
    trace.append(f"R9 unhandled CARC {d.carc}")
    return done(DecisionType.ESCALATE, f"No handling rule for CARC {d.carc}; needs a human.", confidence=1.0)