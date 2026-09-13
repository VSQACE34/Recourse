"""Drafts. Templates carry the structure; the LLM's job was upstream (assessment/correction),
so the letter only ever contains facts we already verified. No free-form generation here."""
from __future__ import annotations

from . import config
from .models import Claim, Decision, DecisionType, Denial, PayerRules


def appeal_letter(d: Denial, c: Claim, dec: Decision, payer: PayerRules) -> str:
    a = dec.assessment
    lines = [
        f"{config.PRACTICE_NAME} — NPI {config.PRACTICE_NPI}",
        f"To: {payer.name}, Provider Appeals",
        f"Re: First-level appeal — Claim {d.claim_id} — Control # {d.denial_id}",
        f"Patient: {c.patient_name}   Member ID: {c.member_id}   DOS: {c.dos.isoformat()}",
        f"Denied service: CPT {d.procedure or ', '.join(c.cpt)}   Denied amount: ${d.denied_amount:,.2f}   CARC {d.carc}" + (f" / RARC {d.rarc}" if d.rarc else ""),
        "",
        f"We request reconsideration of the above denial. {dec.reason}",
        "",
    ]
    if d.carc_code == "29":
        lines += [
            f"The claim was first submitted on {c.first_submitted} (clearinghouse acceptance reference {c.clearinghouse_ref}), "
            f"which is within {payer.timely_filing_days_from_dos} days of the date of service. The acceptance report is attached as proof of timely filing.",
        ]
    elif a:
        lines.append("Policy criteria and supporting documentation:")
        for crit in a.criteria_met:
            lines.append(f"  • {crit}")
        if a.evidence:
            lines.append("")
            lines.append("From the visit note:")
            for q in a.evidence:
                lines.append(f'  "{q}"')
    lines += [
        "",
        "The complete visit note is attached. Please reprocess the claim for payment.",
        "",
        f"Sincerely,\nBilling Department, {config.PRACTICE_NAME}\n{config.PRACTICE_EMAIL}",
    ]
    return "\n".join(lines)


def resubmission_cover(d: Denial, c: Claim, dec: Decision) -> str:
    corr = dec.correction
    return (
        f"Corrected claim resubmission — {d.claim_id} (original control # {d.denial_id})\n"
        f"Patient: {c.patient_name}   DOS: {c.dos.isoformat()}   CPT: {', '.join(c.cpt)}\n"
        f"Denial: CARC {d.carc}" + (f" / RARC {d.rarc}" if d.rarc else "") + "\n"
        f"Correction: {corr.field} changed from '{corr.old}' to '{corr.new}'.\n"
        f"Reason: {corr.rationale}\n"
        f"Frequency code 7 (replacement of prior claim)."
    )


def escalation_text(d: Denial, c: Claim | None, dec: Decision) -> str:
    who = "Physician review needed" if dec.type == DecisionType.ESCALATE_PHYSICIAN else "Billing team review needed"
    head = f"*{who}* — Claim {d.claim_id} ({d.patient_name}) — CARC {d.carc} — ${(d.denied_amount or 0):,.2f}"
    body = dec.reason
    extra = ""
    if dec.assessment and dec.assessment.criteria_unmet:
        extra = "\nUnmet criteria: " + "; ".join(dec.assessment.criteria_unmet)
    dl = f"\nAppeal deadline: {dec.appeal_deadline} ({dec.days_to_deadline} days)" if dec.appeal_deadline else ""
    return f"{head}\n{body}{extra}{dl}"


def plan_summary(d: Denial, dec: Decision) -> str:
    amt = f"${d.denied_amount:,.2f}" if d.denied_amount is not None else "n/a"
    urg = " 🔴 URGENT" if dec.urgent else ""
    return f"{d.claim_id} · {d.patient_name} · {d.carc} · {amt} → *{dec.type.value}*{urg}\n{dec.reason}"
