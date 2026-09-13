# Denial Agent — rehearsal build

An agent that works medical claim denials: reads the denial, pulls the claim, note and payer policy,
decides **correct & resubmit / appeal / write off / escalate**, drafts the letter, books the deadline,
updates the ledger, and asks a human before doing anything irreversible.

Apps: **Gmail** (denials in, appeals out) · **Google Drive** (reads notes, claims, policies; writes every
letter to `appeals/` before it is sent) · **Google Sheets** (claim ledger) · **Google Calendar** (appeal
deadlines) · **Slack** (approval gate, escalations, daily summary).

This is the *rehearsal* repo. Tonight you rebuild it from a blank folder using this as the design;
the fixtures and the setup you do today are reusable regardless of the rules.

```
fixtures/         denials (17 emails incl. one letter-style + one non-denial), notes, claims, patients, payer policies, ledger.csv
agent/
  models.py       Denial / Claim / Decision / Plan / Action
  denial_parser.py  email -> Denial (regex first; LLM extraction fallback for free-text letters, see D-17)
  triage.py       THE decision engine: rule cascade + LLM only for judgement calls
  llm.py          GroqLLM / OpenAICompatLLM (OpenRouter, Groq, OpenAI, Ollama) + MockLLM (deterministic, offline)
  drafting.py     appeal letter / resubmission cover / escalation text (templates, no free generation)
  planner.py      Decision -> ordered idempotent actions (email -> calendar -> ledger -> chat)
  executor.py     approval gate, retry once, fail-stop, failure notice
  pipeline.py     orchestration + plan registry
  tools/base.py   the 5 interfaces;  tools/mock.py (stateful replicas + fault injection);
                  tools/google_apps.py, tools/slack_app.py (real adapters)
app/main.py       FastAPI: / (dashboard) /ingest /ingest-fixture/{name} /poll /plans/{id}/approve
                  /slack/interactions /slack/poll-reactions /report /report/slack /traces/{id}
app/dashboard.html worklist UI: decision stamp, rule trace, draft letter, approve/reject — use it in the demo
eval/             scenarios.yaml (20 cases) + run_eval.py -> results.md + traces/
scripts/          seed_google.py (mirror fixtures to Drive/Sheets/Gmail), smoke_google.py
```

## Run it now (no credentials)

```bash
pip install -r requirements.txt
python -m eval.run_eval                 # 20/20 with the mock LLM
AGENT_TODAY=2026-09-13 TOOLS=mock uvicorn app.main:app --reload
open http://localhost:8000              # dashboard: pick D-03.txt -> Ingest fixture -> Approve and execute
curl -X POST localhost:8000/ingest-fixture/D-04.txt   # or drive it from the command line
curl localhost:8000/plans ; curl -X POST localhost:8000/plans/<id>/approve ; curl localhost:8000/mock/state
```

## Test the real model

Any OpenAI-compatible endpoint works; pick whichever key you have to hand:

```bash
# OpenRouter
LLM_PROVIDER=openai_compat LLM_BASE_URL=https://openrouter.ai/api/v1 LLM_API_KEY=$OPENROUTER_API_KEY \
LLM_MODEL=meta-llama/llama-3.3-70b-instruct python -m eval.run_eval S01 S02 S03 S04 S07 S08 S11
# Groq (SDK)
LLM_PROVIDER=groq GROQ_API_KEY=... python -m eval.run_eval S01 S02 S03 S04 S07 S08 S11
# Ollama (offline mode for the demo story)
LLM_PROVIDER=openai_compat LLM_BASE_URL=http://localhost:11434/v1 LLM_API_KEY=ollama LLM_MODEL=llama3.1 python -m eval.run_eval S03 S04
```
S03/S04/S08/S20 are the judgement cases, and D-17.txt (a letter-style denial with no field labels) exercises
the LLM extraction fallback — ingest it from the dashboard with a real model to show free-text parsing. If the model is wobbly on S04 (it should *not* support the
10-day back pain MRI), tighten `ASSESS_SYSTEM` in `agent/llm.py` — that prompt is the whole
"reliability" story, iterate on it today, not tonight.

## Set up the real apps (do this today)

**Google (one project, one consent screen, four APIs)** — scopes are gmail.modify, drive (full, so the agent
can write letters back), spreadsheets, calendar.events. If you already have a `token.json` from an earlier
scope set, delete it and re-consent.
1. console.cloud.google.com → new project `denial-agent` → APIs & Services → enable **Gmail API,
   Drive API, Sheets API, Calendar API**.
2. OAuth consent screen → External → add yourself as a test user → scopes: leave default (the app
   requests them at runtime).
3. Credentials → Create → OAuth client ID → **Desktop app** → download JSON → save as `credentials.json`.
4. `cp .env.example .env`, fill `PRACTICE_EMAIL` with your Gmail, then:
   ```bash
   python -m scripts.seed_google --drive --sheet --email you@gmail.com
   ```
   First run opens the browser for consent and writes `token.json`. It prints `DRIVE_FOLDER_ID`
   and `SHEET_ID` → put them in `.env`.
5. In Gmail: create label `denials`; create a filter (subject has `Claim Adjustment Notice` OR
   `Remittance Advice` OR `claim determination` → apply label `denials`). Re-run `--email` if the first
   batch missed the filter. The agent creates `denials-processed` itself and uses it, not read/unread
   state, to know what's pending — so opening a denial in Gmail doesn't hide it from the agent.

**Slack**
1. api.slack.com/apps → Create → From scratch → in a throwaway workspace.
2. OAuth & Permissions → Bot Token Scopes: `chat:write`, `chat:write.public` → Install → copy
   `xoxb-` token → `.env`. Basic Information → Signing Secret → `.env`.
3. Create channel `#billing-denials`, invite the bot (`/invite @DenialAgent`).
4. **Buttons (needs a tunnel):** Interactivity & Shortcuts → On → Request URL
   `https://<tunnel>/slack/interactions`. Use `ngrok http 8000` (or cloudflared).
5. **Reactions (no tunnel — the fallback):** add scope `reactions:read`, reinstall the app. React ✅ or ❌
   on the approval card, then `curl -X POST localhost:8000/slack/poll-reactions`, or run the app with
   `APPROVAL_POLL_SECONDS=10` and it polls itself. Test it offline first:
   `curl -X POST localhost:8000/mock/react/<plan_id>/white_check_mark && curl -X POST localhost:8000/slack/poll-reactions`.
   If ngrok misbehaves tonight, demo with reactions — it reads as "human in the loop" just as well.

**Verify**
```bash
TOOLS=google python -m scripts.smoke_google        # one call per app
TOOLS=google LLM_PROVIDER=groq uvicorn app.main:app
curl -X POST localhost:8000/poll                   # real inbox -> real plans -> Slack buttons
```

## Reliability design (this is the brief)

- **Rules decide the category, the LLM decides only judgement.** CARC 18/29/45/PR and deadlines are
  arithmetic; the model is called only to read a note against a policy or find a concrete fix.
- **Every rule that fires is recorded** (`Decision.rule_trace`) and shown in the Slack approval card.
- **Safe failure:** unparseable model output → low-confidence assessment → escalate, never act.
- **Approval gate:** emails and write-offs are `irreversible` → plan waits for a Slack click.
- **Ordering:** email → calendar → ledger → chat. The ledger is written last, so it can never say
  "sent" when nothing was sent. A failed plan leaves the ledger untouched and is safe to re-run.
- **Idempotency:** every action has a key (`<denial control #>:<action>`); Calendar uses a private
  extended property, Gmail a local key store; the ledger records processed denial IDs so a
  re-ingested denial is a no-op.
- **Own record first:** the letter is written to Drive (`appeals/<claim>_<control#>_<kind>.txt`) before the
  email goes out, so there is always a copy of exactly what was sent.
- **Economics and time are rules, not vibes:** below `MIN_APPEAL_AMOUNT` → write off; appeal window
  expired → write off; both gated behind approval, both visible in the rule trace.
- **Unparseable mail is left alone:** the regex parser tries first, then the LLM extractor; if neither
  finds a claim number and a CARC, the message stays in the inbox and the team is told.
- **Evaluation:** 20 seeded scenarios against stateful mocks with fault injection; `eval/results.md`
  is the table; `traces/*.jsonl` are the per-scenario step logs.
- **Submission brief:** `docs/reliability_brief.md` is the one-page template — fill in tonight's numbers.

## Tonight — build order (10:00 PM → 4:30 AM IST)

| Slot | Do | Cut if late |
|---|---|---|
| 10:00–10:20 | Blank repo, `.env`, fixtures copied in, `models.py` | — |
| 10:20–11:00 | `denial_parser.py`, `triage.py` rules only (no LLM yet), `planner.py` | — |
| 11:00–11:40 | `tools/base.py` + `mock.py`, `executor.py`, `pipeline.py`, first eval run | — |
| 11:40–12:20 | `llm.py` with Groq, run S03/S04/S08 against the real model | — |
| 12:20–1:30 | Real adapters: Gmail, Sheets, Slack (reactions first, buttons if ngrok is up) | Calendar; Drive write (keep Drive read, or read fixtures from disk) |
| 1:30–2:15 | End-to-end on real apps with D-03, D-01, D-06, D-10; fix what breaks | — |
| 2:15–3:00 | Full eval run, write `results.md`, screenshot dashboard + Slack card + sheet | dashboard (curl works) |
| 3:00–3:45 | Record 2-min demo (script below), write reliability brief (1 page from this README) | polish |
| 3:45–4:30 | README, push, submit; buffer | — |

Rule of the night: **three real apps + green eval table** beats five apps and a hand-wave.
Gmail + Sheets + Slack is the minimum; Calendar is 20 minutes if there's time; Drive last.

## Demo script (2 minutes)

1. (10s) "Denied claims are the biggest revenue leak in a clinic, and most never get worked."
2. (30s) Dashboard open. Denial email lands → *Poll inbox* → row appears with the stamp *appeal*,
   the rules that fired, and the drafted letter quoting the note. Same card is in Slack.
3. (20s) *Approve and execute* → letter in Drive `appeals/`, appeal in Gmail Sent, calendar deadline,
   ledger row flips to `appealed`. Steps tick off on the row.
4. (25s) The judgement: D-04 same code, thin note → *escalate physician*, nothing sent. D-16 no prior
   auth but the note documents an emergency → appeal under the policy's exception. D-13 window closed
   → write-off, gated. D-15 newsletter in the denials label → untouched.
5. (25s) `eval/results.md`: 20 scenarios incl. duplicate ingestion, Gmail outage, rejected plan,
   missing note, garbage mail. Open one trace. *Post summary to Slack* → dollars by decision.
6. (10s) "Rules decide the category, the model decides only judgement, a human approves anything
   irreversible."
