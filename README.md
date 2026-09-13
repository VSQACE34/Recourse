# Recourse — every denied claim gets its recourse

**Live demo:** `https://web-production-ac091.up.railway.app` · **Demo video (2 min):** `https://drive.google.com/file/d/1T3uX4osRruquVy6-LAfcN0IZ-4IDy0cg/view?usp=sharing` · **Slack workspace (to click approve yourself):** `https://join.slack.com/t/throw-testapp/shared_invite/zt-49tfonuhd-RKGR4dSaw8awIqgjYFjXBg`

Recourse is an AI agent that works medical claim denials end to end. It reads the denial, the clinical note and the payer's policy, decides whether to **correct and resubmit, appeal, write off, bill the patient, or escalate to a physician**, then does the paperwork across **Gmail, Google Drive, Google Sheets, Google Calendar and Slack**. A human approves anything irreversible. Every decision was evaluated against 20 seeded scenarios, on a deterministic mock model and on the real one, before it sent a single real email.

Built solo by George A. Johny for the Multi-App AI Agent Hackathon (Lemma × Comma Capital, judged by Arga Labs), 13 September 2026.

---

## 01 · Project overview

### The problem

A denied claim is a payer refusing to pay for care that has already been delivered. Working one means reading the reason code, finding the clinical note, checking the payer's policy, drafting a letter, tracking a deadline and updating a ledger: about twenty minutes of skilled staff time per claim.

The scale of it, from industry sources compiled in 2026:

- **11.8%** of claims are denied on first submission (Experian Health, 2024 all-payer rate), up from 10.2% in 2020.
- **~70%** of denials that are appealed get overturned and paid (Premier Inc.).
- **65%** of denied claims are never resubmitted or appealed at all (MGMA / AHA).
- **$25–$118** staff cost to rework one denial; **$57.23** average administrative cost per denied claim in 2023 (CAQH / Premier).
- The three most common denial codes are CARC 197 (no prior authorisation), 11 (diagnosis inconsistent with procedure) and 16 (missing information).

Put together: the money is recoverable, the process to recover it is well understood, and most of it is still written off because a triage task sits between the denial and the appeal.

### What exists today

The denial-management market splits into three kinds of product:

1. **Prediction and prevention** (Waystar, Experian Health, FinThrive, AKASA): score claims before submission so staff fix them first. Good for the 60–70% of denials that are avoidable; does nothing for the ones that arrive.
2. **Appeal drafting** (most "AI denials" modules, AppealGen for patients): generate the letter fast; a human still triages, decides and sends.
3. **Queue-working agents** (Waystar's denials module, DataRovers, CombineHealth, Ventus): autonomously work denials, but enterprise-priced, EHR-integrated, and sold to hospital systems. Rivet Resolve targets smaller practices but is still a platform to adopt.

What none of them do visibly: decide *not* to appeal and say why, show which rule made the decision, or publish how the agent was tested before it touched a real inbox. And the long tail — the small practice running on Gmail and a spreadsheet — has no option at all.

### How Recourse helps

- **It triages, not just drafts.** Five outcomes, including "this is not worth appealing" and "a physician needs to look at this", with the reason on the card.
- **Rules decide the category and the arithmetic; the model only reads.** Duplicates, timely-filing maths, appeal windows, dollar thresholds and member-ID mismatches are deterministic. The language model is called for exactly two reading tasks — does this note meet this policy's criteria, and what is the one concrete fix for a rejected claim — and it must quote the note. Every rule that fired is shown on the Slack card and the dashboard.
- **It acts across the tools a small practice already has.** No EHR integration: Gmail, Drive, Sheets, Calendar, Slack.
- **A human signs anything irreversible.** Emails and write-offs wait for an approve click, in Slack or on the dashboard.
- **It proves what it did.** After execution the app reads back from each API — the sent message from Gmail, the sheet row, the calendar event, the Drive file, the Slack permalink — and shows the receipts.

### What it looks like

`/` is the landing page with the story. `/app` is the worklist: a decision stamp per denial, the rules that fired, the drafted letter, approve/reject, receipts, and an evidence panel with live view-only embeds of the ledger sheet, the calendar, the Drive folder and the Slack channel. `/eval` is the results table.

---

## 02 · External apps used

| App | Reads | Writes |
|---|---|---|
| **Gmail** | Denial notices under the `denials` label (regex parse; LLM extraction fallback for letter-style mail) | Appeal and resubmission emails to the payer; `denials-processed` label |
| **Google Drive** | Clinical notes, original claims, patient records, payer policy documents | Every letter to `appeals/<claim>_<control#>_<kind>.txt`, *before* the email is sent |
| **Google Sheets** | The claim ledger (status, decision, processed denial IDs, deadlines) | Ledger row per claim, written last so it can never claim an email that wasn't sent |
| **Google Calendar** | Existing deadline events (idempotency check) | Appeal deadline with reminder; flagged urgent under 14 days |
| **Slack** | Button clicks (Socket Mode) and ✅/❌ reactions; channel history for the dashboard feed | Approval cards, physician escalations, failure notices, daily summary |

The LLM is Groq (`openai/gpt-oss-120b`); any OpenAI-compatible endpoint works via env vars.

---

## 03 · Setup instructions

### Run it in two minutes, no credentials (mock apps, deterministic model)

```bash
git clone <repo> && cd recourse
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m eval.run_eval                                  # expect 20/20
uvicorn app.main:app --reload                            # http://localhost:8000
```

Open the worklist, ingest `D-03.txt` (clean appeal), then `D-04.txt` (same code, thin note → physician), `D-13.txt` (closed window → write-off), `D-15.txt` (a newsletter → ignored). Approve one and expand *Receipts*.

### Run it with a real model

```
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...
GROQ_MODEL=openai/gpt-oss-120b
```

Copy `.env.example` to `.env`, fill those in, and `python -m eval.run_eval` again. Any OpenAI-compatible endpoint also works: `LLM_PROVIDER=openai_compat` with `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`.

### Run it against real Google Workspace and Slack

**Google** — one Cloud project, four APIs, one Desktop OAuth client.
1. console.cloud.google.com → new project → APIs & Services → enable **Gmail, Drive, Sheets, Calendar** APIs.
2. OAuth consent screen → External → add your Gmail as a **test user**.
3. Credentials → OAuth client ID → **Desktop app** → download → save as `credentials.json` in the project root.
4. Set `PRACTICE_EMAIL` in `.env`, then seed the account with the fixtures (opens the browser for consent, writes `token.json`, prints the IDs to put in `.env`):
   ```bash
   python -m scripts.seed_google --drive --sheet --email you@gmail.com
   ```
5. In Gmail, create a label `denials` and apply it to the seeded emails (plus a filter on subject `Claim Adjustment Notice` / `Remittance Advice` / `claim determination` for future ones). The agent tracks pending work with its own `denials-processed` label, not read/unread state.

**Slack**
1. api.slack.com/apps → Create New App → From scratch.
2. OAuth & Permissions → Bot Token Scopes: `chat:write`, `chat:write.public`, `reactions:read`, `channels:read`, `channels:history` → Install → copy the `xoxb-` token to `.env` as `SLACK_BOT_TOKEN`.
3. Settings → Socket Mode → On → create an app-level token with `connections:write` → `.env` as `SLACK_APP_TOKEN`. Features → Interactivity → On (no URL needed). This makes the approval buttons work with no public URL.
4. Create `#billing-denials` and `/invite @Recourse`.

**Verify and run**
```bash
TOOLS=google python -m scripts.smoke_google     # one call per app
TOOLS=google uvicorn app.main:app --reload      # then Poll inbox on the worklist
python -m scripts.reset_google --calendar       # restore ledger + inbox between demo runs
```

### Hosting

`Procfile` and `render.yaml` are included. On Railway or Render: connect the repo, set the variables from `.env.example`, and paste the contents of `token.json` into `GOOGLE_TOKEN_JSON` (the app writes it to disk at startup). Share the ledger sheet and Drive folder as *anyone with the link, viewer* and make the calendar public so the evidence panel can embed them; set `SLACK_INVITE_URL` for the join link.

### Layout

```
agent/
  denial_parser.py  email -> Denial (regex; LLM extraction fallback)
  triage.py         the decision engine: rule cascade R1–R9, model only for judgement
  llm.py            Groq / OpenAI-compatible clients + deterministic MockLLM
  drafting.py       appeal letter, resubmission cover, escalation text (templates)
  planner.py        Decision -> ordered idempotent actions (Drive -> Gmail -> Calendar -> Sheets -> Slack)
  executor.py       approval gate, retry once, fail-stop, failure notice
  pipeline.py       orchestration, plan registry, dedup, reaction polling, receipts, reports
  tools/            base.py (5 interfaces) · mock.py (stateful replicas + fault injection) · google_apps.py · slack_app.py · slack_socket.py
app/                main.py (FastAPI) · landing.html · dashboard.html
eval/               scenarios.yaml (20) · run_eval.py · results.md
fixtures/           17 denial emails, notes, claims, patients, 4 payer policies, ledger.csv
scripts/            seed_google.py · smoke_google.py · reset_google.py
docs/               reliability_brief.md
```

---

## 04 · Reliability testing

### Design choices that make it safe to let it act

| Property | Mechanism |
|---|---|
| Never acts on a claim it can't find | R1: unknown claim → escalate, zero side effects |
| Never double-works a denial | Plan-level dedup on ingest; ledger records processed denial IDs; re-checked at approval time |
| Never appeals on thin evidence | Model must quote the note; confidence < 0.70 → physician review, never an automatic write-off |
| Never sends without a human | Emails and write-offs are `irreversible` → Slack buttons / reactions / dashboard approve |
| Ledger can't lie | Action order Drive → Gmail → Calendar → Sheets → Slack; ledger written last; a failed plan leaves it untouched |
| Failures are visible | Retry once, then stop, mark the plan failed, notify the channel with the failed step |
| Model garbage can't cause action | Unparseable JSON → confidence 0 → escalate |
| Economics and deadlines are arithmetic | Below-threshold and expired-window denials are written off by rule, gated, and traced |
| Non-denials can't trigger anything | No claim number + CARC → message left in inbox, team told |
| Every step is replayable | Idempotency keys on every side effect; JSONL trace per run (`traces/`) |
| It proves what it did | Read-after-write receipts from each API on every executed plan |

### The evaluation

`python -m eval.run_eval` runs 20 seeded scenarios against stateful replicas of the five apps with fault injection. Each scenario starts from a clean world, ingests one or two denial emails, and asserts the decision, the number of emails sent, calendar events, ledger updates, plan status, and message contents.

| Category | Scenarios |
|---|---|
| Coding fixes | S01 member ID mismatch (deterministic rule) · S02 missing laterality modifier (model reads the note) |
| Judgement | S03 criteria met → appeal · S04 criteria not met → physician, no write-off · S08 bundled E/M with separate problem → modifier 25 appeal · S20 no prior auth but documented emergency → appeal under the policy exception |
| Arithmetic | S05 duplicate, original paid · S06 late filing, no proof → write off · S07 late filing with clearinghouse proof → appeal · S09 copay → patient · S11 deadline in 3 days → urgent · S16 $18 below threshold → write off · S17 window closed 3 days ago → write off |
| Safety | S10 claim not on ledger → touch nothing · S18 note missing → escalate · S19 newsletter in the denials label → left alone · S15 rejected plan executes nothing |
| Idempotency and faults | S12 same denial twice → one email · S13 Gmail fails once → retried, ledger updated · S14 Gmail fails twice → ledger untouched, team notified |

**Results:** 20/20 on the deterministic mock model · 20/20 on Groq `openai/gpt-oss-120b`. Table in [`eval/results.md`](eval/results.md), rendered at `/eval` on the live app; per-scenario traces in `traces/`.

### What the eval changed

Two findings during rehearsal changed the design, which is the point of having one:

1. **S02 failed on the real model** because the correction prompt asked for a bare `null` when no fix exists, which the provider's JSON mode rejects, and because a *missing* modifier has no "old" value. The contract was fixed to an object-only shape and the parser made tolerant. The failure mode was already correct (escalate), but the answer was wrong.
2. **S14 failed once in three runs** because the model returned no correction for a member-ID mismatch on identical input. A member-ID mismatch is a comparison, not a reading task, so it became rule **R7a** and the model is no longer asked. This is the general principle of the system: when the model is wrong on something deterministic, take the decision away from it rather than prompt harder.

The full one-page brief is in [`docs/reliability_brief.md`](docs/reliability_brief.md).

---

## 05 · Demo video and demo link

**https://drive.google.com/file/d/1T3uX4osRruquVy6-LAfcN0IZ-4IDy0cg/view?usp=sharing** (under two minutes)

demo link : - web-production-ac091.up.railway.app

---

## Limitations and next steps

Policies are read as text, not structured rules. Only CARC codes with a rule are handled; everything else escalates, by design. Appeal outcomes are not yet tracked back into the ledger. Plans live in memory (restart clears them; the ledger and inbox are the durable state). Next: payer-specific appeal templates, second-level appeals, learning thresholds from which appeals actually got paid, and running the same scenario set nightly against replayed production traffic.