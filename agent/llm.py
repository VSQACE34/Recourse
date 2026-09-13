"""LLM layer. Two implementations behind one interface:

- GroqLLM: real calls, JSON mode, retries, and a *safe failure* — if the model
  returns garbage we hand back a low-confidence assessment so the pipeline
  escalates instead of acting.
- MockLLM: deterministic keyword heuristics so the eval harness runs offline and
  the plumbing (rules, planner, executor, idempotency, faults) is tested
  independently of model behaviour. Swap LLM_PROVIDER=groq to test the model.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional, Protocol

from .config import GROQ_MODEL, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_PROVIDER
from .models import Assessment, Claim, Correction, Denial, Patient
from .tracing import NULL_TRACER, Tracer


class LLM(Protocol):
    name: str

    def assess_documentation(self, denial: Denial, claim: Claim, note: str, policy: str, tracer: Tracer) -> Assessment: ...
    def propose_correction(self, denial: Denial, claim: Claim, patient: Optional[Patient], note: str, tracer: Tracer) -> Optional[Correction]: ...
    def polish(self, text: str, tracer: Tracer) -> str: ...
    def extract_denial(self, subject: str, body: str, tracer: Tracer) -> Optional[dict]: ...


# ----------------------------------------------------------------------------
# Prompts (shared by Groq; kept here so they are easy to iterate on)
# ----------------------------------------------------------------------------

ASSESS_SYSTEM = """You are a medical billing appeals specialist. You are given a payer denial,
the claim, the clinical note, and the payer's written coverage policy. Decide whether the
documentation in the note satisfies the policy criteria for the denied service.

Be strict: only count a criterion as met if the note explicitly documents it. Quote the note.
Never invent findings. If the note is thin or ambiguous, say so with low confidence.

Respond with ONLY a JSON object:
{
  "supported": true|false,
  "confidence": 0.0-1.0,
  "criteria_met": ["..."],
  "criteria_unmet": ["..."],
  "evidence": ["short verbatim quote from note", ...],
  "rationale": "two or three sentences"
}"""

CORRECTION_SYSTEM = """You are a medical claims specialist fixing a rejected claim. Given the denial
reason (CARC/RARC and its description), the claim as submitted, the patient record on file, and the
clinical note, identify the single concrete correction that would resolve the rejection, if it is
determinable from the data provided. Do not guess.

Typical determinable fixes:
- Member ID on the claim differs from the patient record -> field "member_id", new = the ID on file.
- A required laterality modifier is missing and the note documents the side -> field "modifier",
  new = "LT" or "RT" (old = "(none)").
- Diagnosis or CPT on the claim contradicts what the note documents -> field "icd10" or "cpt".

Respond with ONLY a JSON object of this exact shape:
{"correction": {"field": "member_id|modifier|icd10|cpt|dos", "old": "(none) if absent", "new": "...", "rationale": "..."}}
If no correction can be determined from the data, respond with exactly {"correction": null}."""


EXTRACT_SYSTEM = """You extract structured fields from a health-plan claim denial or remittance message.
Return ONLY a JSON object with these keys (use null when absent, never guess):
{"claim_id": "practice claim number, e.g. C-1003", "control_number": "payer claim control / ICN, or null",
 "payer": "payer name as written", "patient_name": "...", "member_id": "...", "date_of_service": "YYYY-MM-DD",
 "procedure": "CPT code", "billed": number, "paid": number, "denied_amount": number,
 "carc": "group-code like CO-50 or PR-3", "rarc": "remark code like N115 or null",
 "remittance_date": "YYYY-MM-DD", "description": "denial reason text"}
If the message is not a claim denial or remittance at all, return {"not_a_denial": true}."""


def _json_only(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    return json.loads(text)


# ----------------------------------------------------------------------------
# Groq
# ----------------------------------------------------------------------------

class _RemoteLLM:
    """Shared prompt/parse logic. Subclasses only implement _chat()."""
    name = "remote"
    model = ""

    def _chat(self, system: str, user: str, tracer: Tracer, json_mode: bool = True, retries: int = 2) -> str:
        raise NotImplementedError

    def assess_documentation(self, denial, claim, note, policy, tracer):
        user = (f"DENIAL:\n{denial.model_dump_json(indent=1)}\n\nCLAIM:\n{claim.model_dump_json(indent=1)}\n\n"
                f"POLICY:\n{policy}\n\nCLINICAL NOTE:\n{note}")
        try:
            data = _json_only(self._chat(ASSESS_SYSTEM, user, tracer))
            a = Assessment(**data)
        except Exception as e:  # noqa: BLE001
            tracer.log("llm.assess.parse_failed", error=str(e))
            a = Assessment(supported=False, confidence=0.0, rationale=f"LLM output unusable: {e}")
        tracer.log("llm.assess", result=a)
        return a

    def propose_correction(self, denial, claim, patient, note, tracer):
        user = (f"DENIAL:\n{denial.model_dump_json(indent=1)}\n\nCLAIM AS SUBMITTED:\n{claim.model_dump_json(indent=1)}\n\n"
                f"PATIENT RECORD ON FILE:\n{patient.model_dump_json(indent=1) if patient else 'none'}\n\nCLINICAL NOTE:\n{note}")
        raw = ""
        try:
            raw = self._chat(CORRECTION_SYSTEM, user, tracer)
            data = _json_only(raw)
            if isinstance(data, dict) and "correction" in data:      # documented shape
                data = data["correction"]
            if data and isinstance(data, dict) and data.get("field") and data.get("new"):
                data = {**data, "old": data.get("old") or "(none)", "rationale": data.get("rationale") or ""}
                c = Correction(**data)
            else:
                c = None
        except Exception as e:  # noqa: BLE001
            tracer.log("llm.correction.parse_failed", error=str(e), raw=raw[:500])
            c = None
        tracer.log("llm.correction", result=c)
        return c

    def extract_denial(self, subject, body, tracer):
        try:
            data = _json_only(self._chat(EXTRACT_SYSTEM, f"SUBJECT: {subject}\n\nBODY:\n{body}", tracer))
        except Exception as e:  # noqa: BLE001
            tracer.log("llm.extract.parse_failed", error=str(e))
            return None
        tracer.log("llm.extract", result=data)
        if not isinstance(data, dict) or data.get("not_a_denial"):
            return None
        return data

    def polish(self, text, tracer):
        # Optional: tidy a templated letter. Kept off the critical path; failure returns the template.
        try:
            return self._chat("Lightly edit this appeal letter for clarity and professionalism. Do not add facts. Return only the letter.",
                              text, tracer, json_mode=False)
        except Exception:  # noqa: BLE001
            return text


class GroqLLM(_RemoteLLM):
    """Groq via the official SDK. LLM_PROVIDER=groq, GROQ_API_KEY, GROQ_MODEL."""
    name = "groq"

    def __init__(self, model: str = GROQ_MODEL):
        from groq import Groq  # lazy import so mock runs need no SDK
        self.client = Groq(api_key=os.environ["GROQ_API_KEY"])
        self.model = model

    def _chat(self, system, user, tracer, json_mode=True, retries=2):
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                with tracer.span("llm.call", provider=self.name, model=self.model, attempt=attempt, system=system[:60]):
                    kwargs = dict(model=self.model, temperature=0.1,
                                  messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
                    if json_mode:
                        kwargs["response_format"] = {"type": "json_object"}
                    resp = self.client.chat.completions.create(**kwargs)
                    return resp.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001
                last_err = e
        raise RuntimeError(f"LLM failed after retries: {last_err}")


class OpenAICompatLLM(_RemoteLLM):
    """Any OpenAI-compatible chat endpoint via plain httpx. LLM_PROVIDER=openai_compat.

    OpenRouter: LLM_BASE_URL=https://openrouter.ai/api/v1  LLM_MODEL=meta-llama/llama-3.3-70b-instruct
    Groq:       LLM_BASE_URL=https://api.groq.com/openai/v1 LLM_MODEL=llama-3.3-70b-versatile
    OpenAI:     LLM_BASE_URL=https://api.openai.com/v1      LLM_MODEL=gpt-4o-mini
    Ollama:     LLM_BASE_URL=http://localhost:11434/v1      LLM_MODEL=llama3.1  LLM_API_KEY=ollama
    """
    name = "openai_compat"

    def __init__(self, base_url: str = LLM_BASE_URL, api_key: str = LLM_API_KEY, model: str = LLM_MODEL):
        import httpx  # lazy
        if not base_url or not model:
            raise RuntimeError("LLM_BASE_URL and LLM_MODEL are required for LLM_PROVIDER=openai_compat")
        self.base_url, self.model = base_url.rstrip("/"), model
        self.http = httpx.Client(timeout=60, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})

    def _chat(self, system, user, tracer, json_mode=True, retries=2):
        last_err: Optional[Exception] = None
        body = {"model": self.model, "temperature": 0.1,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        for attempt in range(retries + 1):
            try:
                with tracer.span("llm.call", provider=self.name, model=self.model, attempt=attempt, system=system[:60]):
                    r = self.http.post(f"{self.base_url}/chat/completions", json=body)
                    if r.status_code == 400 and json_mode and "response_format" in body:
                        body.pop("response_format")           # some providers/models reject JSON mode; prompt already says JSON-only
                        r = self.http.post(f"{self.base_url}/chat/completions", json=body)
                    r.raise_for_status()
                    return r.json()["choices"][0]["message"]["content"] or ""
            except Exception as e:  # noqa: BLE001
                last_err = e
        raise RuntimeError(f"LLM failed after retries: {last_err}")


# ----------------------------------------------------------------------------
# Mock — deterministic stand-in for offline evals
# ----------------------------------------------------------------------------

class MockLLM:
    name = "mock"

    def assess_documentation(self, denial, claim, note, policy, tracer):
        n = note.lower()
        code = claim.cpt[0] if claim.cpt else ""
        met, unmet, ev = [], [], []

        if code in {"72148", "72149", "72158"}:                       # lumbar MRI policy
            weeks = [int(w) for w in re.findall(r"(\d+)\s*weeks?", n)]
            dur_ok = any(w >= 6 for w in weeks)
            cons_ok = any(k in n for k in ("physical therapy", "naproxen", "ibuprofen", "nsaid", "conservative"))
            neuro_ok = ("slr positive" in n or "4/5" in n or "deficit" in n or "weakness" in n)
            for ok, label, quote in [
                (dur_ok, "symptoms >= 6 weeks", "9 weeks" if dur_ok else ""),
                (cons_ok, ">= 6 weeks conservative therapy", "7 weeks of physical therapy" if cons_ok else ""),
                (neuro_ok, "neurological deficit / positive SLR", "SLR POSITIVE on right at 40°" if neuro_ok else ""),
            ]:
                (met if ok else unmet).append(label)
                if quote:
                    ev.append(quote)
            supported = dur_ok and cons_ok and neuro_ok
            conf = 0.92 if supported else 0.88
        elif code in {"93000", "93005", "93010"}:                     # ECG policy
            sym = any(k in n for k in ("chest", "palpitation", "syncope", "dyspn"))
            (met if sym else unmet).append("cardiac symptoms documented")
            if sym:
                ev.append("Intermittent chest tightness x2 weeks, worse on exertion")
            supported, conf = sym, 0.9
        elif denial.carc_code == "197":                                # prior-auth emergency exception
            emergent = any(k in n for k in ("emergent", "emergency", "walk-in", "arrival:"))
            same_visit = "performed" in n and ("ct" in n or "mri" in n)
            unsafe_delay = "unsafe delay" in n or "delay would have been unsafe" in n
            for ok, label in [(emergent, "emergency presentation documented"), (same_visit, "service performed at the emergency encounter"),
                              (unsafe_delay, "statement that authorisation would have caused unsafe delay")]:
                (met if ok else unmet).append(label)
            if emergent:
                ev.append("Witnessed loss of consciousness of approximately 1 minute")
            if unsafe_delay:
                ev.append("Prior authorisation not obtained: emergent study, delay would have been unsafe")
            supported = emergent and same_visit and unsafe_delay
            conf = 0.9 if supported else 0.85
        elif denial.carc_code == "97":                                 # modifier 25 policy
            distinct = ("problem 1" in n and "problem 2" in n) or "separately identifiable" in n
            mdm = any(k in n for k in ("mdm", "prescription", "increase", "plan:"))
            for ok, label in [(distinct, "distinct problem from procedure"), (mdm, "own history/exam/MDM documented")]:
                (met if ok else unmet).append(label)
            if distinct:
                ev.append("increase amlodipine to 10 mg daily, BMP in 2 weeks")
            supported = distinct and mdm
            conf = 0.86 if supported else 0.8
        else:
            supported, conf = False, 0.4                               # unknown policy -> low confidence
            unmet.append("no matching policy heuristic")

        a = Assessment(supported=supported, confidence=conf, criteria_met=met, criteria_unmet=unmet,
                       evidence=ev, rationale="mock heuristic assessment")
        tracer.log("llm.assess", provider="mock", result=a)
        return a

    def propose_correction(self, denial, claim, patient, note, tracer):
        c = None
        if denial.rarc == "N382" and patient and denial.member_id_submitted and denial.member_id_submitted != patient.member_id:
            c = Correction(field="member_id", old=denial.member_id_submitted, new=patient.member_id,
                           rationale="Submitted member ID does not match the ID on the patient record.")
        elif denial.carc_code == "4":
            n = note.lower()
            side = "LT" if "left" in n else ("RT" if "right" in n else None)
            if side:
                c = Correction(field="modifier", old="(none)", new=side,
                               rationale=f"Note documents the {'left' if side == 'LT' else 'right'} side; laterality modifier required.")
        tracer.log("llm.correction", provider="mock", result=c)
        return c

    def polish(self, text, tracer):
        return text

    def extract_denial(self, subject, body, tracer):
        return None                                                     # mock never guesses; unparseable stays unparseable


def get_llm() -> LLM:
    if LLM_PROVIDER == "groq":
        return GroqLLM()
    if LLM_PROVIDER == "openai_compat":
        return OpenAICompatLLM()
    return MockLLM()