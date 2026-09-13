"""Turn a remittance email into a Denial.

Regex first (ERA-style emails are semi-structured), so the parse is deterministic and
cheap. If the regex can't find a claim number + CARC, and an LLM is available, the LLM is
asked to extract the fields from free text (D-17.txt is a letter-style example). The mock
LLM never guesses, so garbage mail stays unparseable and is left in the inbox.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from .models import Denial
from .tools.base import Message

FIELD = re.compile(r"^\s*([A-Za-z #/]+?):\s*(.*?)\s*$", re.M)


def _money(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    m = re.search(r"-?\$?([\d,]+(?:\.\d+)?)", s)
    return float(m.group(1).replace(",", "")) if m else None


def _date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    return date.fromisoformat(m.group(1)) if m else None


def _payer(sender: str, body: str) -> str:
    if "northstar" in (sender + body).lower():
        return "Northstar Advantage"
    if "meridian" in (sender + body).lower():
        return "Meridian Health Plan"
    return "Unknown Payer"


class ParseError(ValueError):
    pass


def parse_denial(msg: Message, llm=None, tracer=None) -> Denial:
    fields = {k.strip().lower(): v for k, v in FIELD.findall(msg.body)}

    claim_id = fields.get("claim number")
    carc = fields.get("carc")
    if not claim_id or not carc:
        if llm is not None and hasattr(llm, "extract_denial"):
            data = llm.extract_denial(msg.subject, msg.body, tracer or _null_tracer())
            if data and data.get("claim_id") and data.get("carc"):
                return _from_extracted(data, msg)
        raise ParseError(f"could not find claim number / CARC in message {msg.id}")

    remit = _date(fields.get("remittance date"))
    control = fields.get("payer claim control #") or fields.get("payer claim control")
    denial_id = control or Denial.fingerprint(claim_id, carc, remit)

    desc = fields.get("description", "")
    # description can wrap onto following lines until a blank line
    m = re.search(r"^Description:\s*(.*?)(?:\n\s*\n|\Z)", msg.body, re.S | re.M)
    if m:
        desc = " ".join(m.group(1).split())

    return Denial(
        denial_id=denial_id,
        claim_id=claim_id,
        payer=_payer(msg.sender, msg.body),
        patient_name=fields.get("patient", ""),
        member_id_submitted=fields.get("member id submitted") or None,
        dos=_date(fields.get("date of service")),
        procedure=fields.get("procedure") or None,
        billed=_money(fields.get("billed")),
        paid=_money(fields.get("paid")),
        denied_amount=_money(fields.get("denied amount")),
        carc=carc.upper(),
        rarc=(fields.get("rarc") or None) or None,
        description=desc,
        remittance_date=remit,
        raw_source=msg.id,
    )


def _null_tracer():
    from .tracing import NULL_TRACER
    return NULL_TRACER


def _from_extracted(x: dict, msg: Message) -> Denial:
    """Build a Denial from LLM-extracted fields. Payer is still resolved by our own matcher so a
    hallucinated payer name can't route an appeal to the wrong address."""
    remit = _date(x.get("remittance_date"))
    claim_id, carc = str(x["claim_id"]).strip(), str(x["carc"]).strip().upper()
    return Denial(
        denial_id=(x.get("control_number") or Denial.fingerprint(claim_id, carc, remit)),
        claim_id=claim_id,
        payer=_payer(msg.sender, msg.body + " " + str(x.get("payer") or "")),
        patient_name=x.get("patient_name") or "",
        member_id_submitted=x.get("member_id") or None,
        dos=_date(x.get("date_of_service")),
        procedure=(str(x["procedure"]) if x.get("procedure") else None),
        billed=_money(str(x.get("billed"))) if x.get("billed") is not None else None,
        paid=_money(str(x.get("paid"))) if x.get("paid") is not None else None,
        denied_amount=_money(str(x.get("denied_amount"))) if x.get("denied_amount") is not None else None,
        carc=carc,
        rarc=x.get("rarc") or None,
        description=x.get("description") or "",
        remittance_date=remit,
        raw_source=msg.id + " (llm-extracted)",
    )
