"""FastAPI surface.

    TOOLS=mock uvicorn app.main:app --reload          # everything in-memory, no credentials
    TOOLS=google uvicorn app.main:app --reload        # real Gmail/Drive/Sheets/Calendar/Slack

Endpoints
  POST /ingest            body: {"text": "<raw denial email>"}  -> plan (awaiting approval or executed)
  POST /poll              pull unread denials from Gmail (or the mock inbox) and handle each
  GET  /plans             list plans
  GET  /plans/{id}
  POST /plans/{id}/approve
  POST /plans/{id}/reject
  POST /slack/interactions   Slack button callbacks (approve / reject)  [needs a public tunnel]
  POST /slack/poll-reactions apply ✅/❌ reactions on approval cards        [no tunnel needed]
                             set APPROVAL_POLL_SECONDS=10 to have the app poll by itself
  GET  /                  dashboard (worklist, rule traces, drafts, approve/reject)
  GET  /fixtures          names of denial fixtures on disk
  POST /ingest-fixture/{name}   ingest fixtures/denials/<name> (works in mock and google mode)
  GET  /traces/{run_id}   JSONL trace for a run
  GET  /report            counts and dollars by decision, from the ledger
  POST /report/slack      post that summary to the billing channel
  GET  /mock/state        (mock only) what's in the fake mailbox / calendar / ledger / chat
"""
from __future__ import annotations

import json
import os
import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pathlib import Path
from pydantic import BaseModel

from agent import config, pipeline
from agent.llm import get_llm
from agent.tools.base import Message, Toolbox
from agent.tracing import Tracer

app = FastAPI(title="Denial Agent")
LLM = get_llm()
TOOLS_KIND = os.getenv("TOOLS", "mock")


def build_toolbox() -> Toolbox:
    if TOOLS_KIND == "google":
        from agent.tools.google_apps import Drive, GCalendar, Gmail, Sheets, get_creds
        from agent.tools.slack_app import Slack
        creds = get_creds()
        return Toolbox(mail=Gmail(creds), docs=Drive(creds), ledger=Sheets(creds), calendar=GCalendar(creds), chat=Slack(),
                       meta={"kind": "google"})
    from agent.tools.mock import mock_toolbox
    tb, faults = mock_toolbox()
    tb.meta["faults"] = faults
    return tb


TB = build_toolbox()


class IngestBody(BaseModel):
    text: str
    subject: Optional[str] = None
    sender: Optional[str] = None


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


@app.get("/fixtures")
def fixtures():
    return sorted(p.name for p in (config.FIXTURES / "denials").glob("*.txt"))


@app.post("/ingest-fixture/{name}")
def ingest_fixture(name: str):
    p = config.FIXTURES / "denials" / name
    if not p.exists() or p.suffix != ".txt":
        raise HTTPException(404, "no such fixture")
    text = p.read_text(encoding="utf-8")
    subject = next((l.split(":", 1)[1].strip() for l in text.splitlines() if l.startswith("Subject:")), name)
    sender = next((l.split(":", 1)[1].strip() for l in text.splitlines() if l.startswith("From:")), "")
    msg = Message(id=f"fx-{name}-{uuid.uuid4().hex[:4]}", subject=subject, body=text, sender=sender)
    tracer = Tracer()
    plan = pipeline.handle_message(msg, TB, LLM, tracer)
    if plan is None:
        return {"trace": tracer.run_id, "plan": None, "note": "not parseable as a denial; team notified"}
    return {"trace": tracer.run_id, "plan": plan.model_dump(mode="json")}


@app.get("/traces/{run_id}", response_class=PlainTextResponse)
def trace(run_id: str):
    p = Path(config.ROOT) / "traces" / f"{run_id}.jsonl"
    if not p.exists():
        raise HTTPException(404)
    return p.read_text(encoding="utf-8")


@app.get("/report")
def report():
    return pipeline.ledger_report(TB)


@app.post("/report/slack")
def report_slack():
    return pipeline.post_report(TB, Tracer())


@app.get("/health")
def health():
    return {"ok": True, "tools": TB.meta.get("kind"), "llm": LLM.name, "today": config.today().isoformat()}


@app.post("/ingest")
def ingest(b: IngestBody):
    msg = Message(id=f"api-{uuid.uuid4().hex[:6]}", subject=b.subject or "(api)", body=b.text, sender=b.sender or "")
    tracer = Tracer()
    plan = pipeline.handle_message(msg, TB, LLM, tracer)
    if plan is None:
        raise HTTPException(422, "could not parse a denial from the text")
    return {"trace": tracer.run_id, "plan": plan.model_dump(mode="json")}


@app.post("/poll")
def poll():
    tracer = Tracer()
    plans = pipeline.poll_inbox(TB, LLM, tracer)
    return {"trace": tracer.run_id, "plans": [p.model_dump(mode="json") for p in plans]}


@app.get("/plans")
def list_plans(full: int = 0):
    if full:
        return [p.model_dump(mode="json") for p in pipeline.PLANS.values()]
    return [{"plan_id": p.plan_id, "claim_id": p.denial.claim_id, "decision": p.decision.type.value, "status": p.status}
            for p in pipeline.PLANS.values()]


@app.get("/plans/{plan_id}")
def get_plan(plan_id: str):
    p = pipeline.PLANS.get(plan_id)
    if not p:
        raise HTTPException(404)
    return p.model_dump(mode="json")


@app.post("/plans/{plan_id}/approve")
def approve(plan_id: str):
    if plan_id not in pipeline.PLANS:
        raise HTTPException(404)
    return pipeline.approve(plan_id, TB, Tracer()).model_dump(mode="json")


@app.post("/plans/{plan_id}/reject")
def reject(plan_id: str):
    if plan_id not in pipeline.PLANS:
        raise HTTPException(404)
    return pipeline.reject(plan_id, TB, Tracer(), reason="rejected via API").model_dump(mode="json")


@app.post("/slack/interactions")
async def slack_interactions(request: Request):
    raw = await request.body()
    if config.SLACK_SIGNING_SECRET:
        from slack_sdk.signature import SignatureVerifier
        if not SignatureVerifier(config.SLACK_SIGNING_SECRET).is_valid_request(raw, dict(request.headers)):
            raise HTTPException(401, "bad slack signature")
    form = await request.form()
    payload = json.loads(form["payload"])
    action = payload["actions"][0]
    plan_id, what = action["value"], action["action_id"]
    user = payload.get("user", {}).get("username", "someone")
    if plan_id not in pipeline.PLANS:
        return PlainTextResponse("plan not found (server restarted?)")
    tracer = Tracer()
    if what == "approve":
        plan = pipeline.approve(plan_id, TB, tracer)
        text = f"✅ Approved by @{user} — plan {plan_id} {plan.status}"
    else:
        plan = pipeline.reject(plan_id, TB, tracer, reason=f"rejected by {user}")
        text = f"⛔ Rejected by @{user} — no actions taken"
    # Replace the button message so it can't be clicked twice.
    return JSONResponse({"replace_original": True, "text": text})


@app.post("/slack/poll-reactions")
def slack_poll_reactions():
    return {"applied": pipeline.poll_reactions(TB, Tracer())}


@app.post("/mock/react/{plan_id}/{emoji}")
def mock_react(plan_id: str, emoji: str):
    """(mock only) simulate a human reacting on the approval card."""
    if TB.meta.get("kind") != "mock":
        raise HTTPException(400, "not in mock mode")
    p = pipeline.PLANS.get(plan_id)
    if not p or not p.approval_msg:
        raise HTTPException(404, "plan has no approval card")
    TB.chat.react(p.approval_msg["ts"], emoji)
    return {"ok": True}


_POLL = float(os.getenv("APPROVAL_POLL_SECONDS", "0"))
if _POLL > 0:
    import asyncio

    @app.on_event("startup")
    async def _start_poll():
        async def loop():
            while True:
                await asyncio.sleep(_POLL)
                try:
                    pipeline.poll_reactions(TB, Tracer())
                except Exception:  # noqa: BLE001
                    pass
        asyncio.create_task(loop())


@app.get("/mock/state")
def mock_state():
    if TB.meta.get("kind") != "mock":
        raise HTTPException(400, "not in mock mode")
    return {
        "inbox_unread": [m.id for m in TB.mail.list_unread()],
        "sent": TB.mail.sent,
        "calendar": TB.calendar.events,
        "chat": TB.chat.messages,
        "ledger": [r.model_dump() for r in TB.ledger.all()],
    }
