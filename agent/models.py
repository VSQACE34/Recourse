"""Core data models. Everything the agent reasons about is one of these."""
from __future__ import annotations

import hashlib
from datetime import date
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------- inputs ----------

class Denial(BaseModel):
    denial_id: str                      # payer control number, or a fingerprint if absent
    claim_id: str
    payer: str
    patient_name: str
    member_id_submitted: Optional[str] = None
    dos: Optional[date] = None
    procedure: Optional[str] = None
    billed: Optional[float] = None
    paid: Optional[float] = None
    denied_amount: Optional[float] = None
    carc: str                           # e.g. "CO-50"
    rarc: Optional[str] = None          # e.g. "N115"
    description: str = ""
    remittance_date: Optional[date] = None
    raw_source: str = ""                # e.g. gmail message id / file name

    @property
    def carc_code(self) -> str:
        """'CO-50' -> '50'; 'PR-3' -> '3'."""
        return self.carc.split("-")[-1].strip()

    @property
    def carc_group(self) -> str:
        """'CO-50' -> 'CO'."""
        return self.carc.split("-")[0].strip().upper()

    @staticmethod
    def fingerprint(claim_id: str, carc: str, remit: Optional[date]) -> str:
        key = f"{claim_id}|{carc}|{remit.isoformat() if remit else ''}"
        return "FP-" + hashlib.sha1(key.encode()).hexdigest()[:10]


class Claim(BaseModel):
    claim_id: str
    patient_id: str
    patient_name: str
    member_id: str
    payer: str
    dos: date
    cpt: list[str]
    modifiers: list[str] = Field(default_factory=list)
    icd10: list[str]
    billed: float
    first_submitted: Optional[date] = None
    clearinghouse_ref: Optional[str] = None
    note_file: Optional[str] = None


class Patient(BaseModel):
    patient_id: str
    name: str
    dob: date
    member_id: str
    payer: str


class LedgerRow(BaseModel):
    claim_id: str
    patient_name: str = ""
    payer: str = ""
    dos: str = ""
    cpt: str = ""
    billed: str = ""
    status: str = ""
    paid_amount: str = ""
    paid_date: str = ""
    denial_ids: str = ""       # ';'-separated
    decision: str = ""
    last_action: str = ""
    next_deadline: str = ""
    notes: str = ""

    def has_denial(self, denial_id: str) -> bool:
        return denial_id in [d for d in self.denial_ids.split(";") if d]


class PayerRules(BaseModel):
    name: str
    timely_filing_days_from_dos: int
    appeal_window_days_from_denial: int
    appeals_email: str
    claims_email: str
    policies: dict[str, str] = Field(default_factory=dict)   # carc code -> doc path


# ---------- decisions ----------

class DecisionType(str, Enum):
    CORRECT_RESUBMIT = "correct_resubmit"
    APPEAL = "appeal"
    WRITE_OFF = "write_off"
    CLOSE_DUPLICATE = "close_duplicate"
    PATIENT_RESPONSIBILITY = "patient_responsibility"
    ESCALATE = "escalate"            # to billing team (missing data, unknown claim, low confidence)
    ESCALATE_PHYSICIAN = "escalate_physician"  # documentation doesn't support; needs clinical input
    NOOP = "noop"                    # already processed


class Assessment(BaseModel):
    """LLM judgement on whether documentation supports the service against policy."""
    supported: bool
    confidence: float = Field(ge=0, le=1)
    criteria_met: list[str] = Field(default_factory=list)
    criteria_unmet: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)   # short quotes from the note
    rationale: str = ""


class Correction(BaseModel):
    field: str            # e.g. "member_id", "modifier"
    old: str = "(none)"   # "(none)" when the field was absent on the original claim
    new: str
    rationale: str = ""


class Decision(BaseModel):
    type: DecisionType
    reason: str
    confidence: float = 1.0
    urgent: bool = False
    appeal_deadline: Optional[date] = None
    days_to_deadline: Optional[int] = None
    assessment: Optional[Assessment] = None
    correction: Optional[Correction] = None
    rule_trace: list[str] = Field(default_factory=list)   # which rules fired, in order


# ---------- plan / execution ----------

class ActionType(str, Enum):
    SAVE_DOCUMENT = "save_document"      # Drive: persist the letter we're about to send
    UPDATE_LEDGER = "update_ledger"
    SEND_EMAIL = "send_email"
    CREATE_CALENDAR_EVENT = "create_calendar_event"
    POST_CHAT = "post_chat"


class Action(BaseModel):
    type: ActionType
    idempotency_key: str
    payload: dict[str, Any]
    irreversible: bool = False       # requires approval before execution
    status: str = "pending"          # pending | done | failed | skipped
    attempts: int = 0
    error: Optional[str] = None
    result: Optional[dict[str, Any]] = None


class Plan(BaseModel):
    plan_id: str
    denial: Denial
    claim: Optional[Claim] = None
    decision: Decision
    summary: str
    draft_text: Optional[str] = None   # appeal letter / resubmission summary / escalation message
    actions: list[Action] = Field(default_factory=list)
    status: str = "awaiting_approval"   # awaiting_approval | approved | rejected | executed | noop
    created_by: str = "denial-agent"
    approval_msg: Optional[dict] = None  # {"channel":..., "ts":...} of the Slack approval card (for reaction polling)
    created_at: float = Field(default_factory=lambda: __import__("time").time())

    @property
    def needs_approval(self) -> bool:
        return any(a.irreversible for a in self.actions)