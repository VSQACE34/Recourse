# System & Reliability Brief — Denial Agent

**One line.** An agent that works medical claim denials: it reads the denial, the claim, the clinical note and the
payer's policy, decides *correct & resubmit / appeal / write off / bill patient / escalate*, drafts the letter, books
the deadline, updates the ledger — and a human approves anything irreversible.

**Apps.** Gmail (denials in, appeals out) · Google Drive (reads notes, claims, policies; writes every letter before it
is sent) · Google Sheets (claim ledger) · Google Calendar (appeal deadlines) · Slack (approval gate, escalations, summary).

## Architecture (one paragraph)
Denial email → parser (regex, LLM fallback) → context load (claim, ledger row, note, policy) → **triage** → **plan** →
**approval gate** → **executor** → ledger + trace. Triage is a deterministic rule cascade (R1–R9) that decides the
*category* and everything arithmetic (deadlines, duplicates, proof of filing, economics); the LLM is called only for
two reading-comprehension tasks — does the note meet the policy criteria, and what is the concrete fix for a rejected
claim — and both are gated by confidence thresholds. Every rule that fires is recorded and shown on the Slack card.

## Why it is safe to let it act
| Property | Mechanism |
|---|---|
| Never acts on a claim it can't find | R1: unknown claim → escalate, zero side effects |
| Never double-works a denial | R2: ledger records processed denial IDs; re-ingest → no-op |
| Never appeals on thin evidence | LLM must quote the note; confidence < 0.70 → physician review, not write-off |
| Never sends without a human | Emails and write-offs are `irreversible` → Slack approve/reject (buttons or ✅/❌) |
| Ledger can't lie | Action order email → calendar → ledger; ledger written last; a failed plan leaves it untouched |
| Failures are visible | Retry once, then stop, mark plan failed, notify channel with the failed step |
| Model garbage can't cause action | Unparseable JSON → confidence 0 → escalate |
| Every step is replayable | Idempotency keys on every side effect; JSONL trace per run |
| Always a copy of what was sent | Letter written to Drive `appeals/` as the first action, before Gmail |
| Economics and deadlines are arithmetic | Below-threshold and expired-window denials are written off by rule, gated, and traced |
| Non-denials can't trigger anything | Regex parser, then LLM extractor; no claim number + CARC → message left in inbox, team told |

## Evaluation
20 seeded scenarios against stateful replicas of the five apps, with fault injection. Categories: coding fixes
(S01–S02), judgement calls where the model must appeal / refuse to appeal (S03, S04, S08, S20), arithmetic rules
(S05–S07, S09, S11, S16 below threshold, S17 expired window), safety (S10 unknown claim, S18 missing note, S19
non-denial email), idempotency (S12), tool faults (S13 retry succeeds, S14 retry fails → ledger untouched),
rejected plan (S15).

**Results (mock LLM, deterministic):** __/20.  **Results (real model, <model name>):** __/20 — failures: ______.
Full table: `eval/results.md`. Traces: `traces/*.jsonl`.

## What we'd do next
Payer-specific appeal templates; second-level appeals; learn thresholds from outcome data (which appeals actually
got paid); run the same scenario set nightly against production traffic replays.

## Known limitations
Policies are read as text, not structured rules; only CARC codes with a rule are handled (everything else escalates);
appeal outcomes are not yet tracked back into the ledger.
