# Eval results — 20/20 passed (llm=groq)

| ID | Scenario | Decision | Emails | Docs | Cal | Ledger | Result |
|---|---|---|---|---|---|---|---|
| S01 | Missing member ID -> correct & resubmit | correct_resubmit | 1 | 1 | 0 | resubmitted | ✅ |
| S02 | Laterality modifier missing -> correct & resubmit with LT | correct_resubmit | 1 | 1 | 0 | resubmitted | ✅ |
| S03 | MRI medical necessity, note meets all criteria -> appeal | appeal | 1 | 1 | 1 | appealed | ✅ |
| S04 | MRI medical necessity, note does NOT meet criteria -> physician review, no auto write-off | escalate_physician | 0 | 0 | 1 | needs_review | ✅ |
| S05 | Duplicate denial, ledger shows original paid -> close, no email | close_duplicate | 0 | 0 | 0 | paid | ✅ |
| S06 | Timely filing, filed late, no proof -> write off (approval-gated) | write_off | 0 | 0 | 0 | written_off | ✅ |
| S07 | Timely filing but clearinghouse proof exists -> appeal with proof | appeal | 1 | 1 | 1 | appealed | ✅ |
| S08 | Bundled E/M (CO-97), note shows separately identifiable problem -> appeal (modifier 25) | appeal | 1 | 1 | 1 | appealed | ✅ |
| S09 | Copay (PR-3) -> patient responsibility, no appeal | patient_responsibility | 0 | 0 | 0 | patient_balance | ✅ |
| S10 | Claim not on ledger -> escalate, touch nothing | escalate | 0 | 0 | 0 | None | ✅ |
| S11 | Appeal deadline in 3 days -> appeal flagged URGENT | appeal | 1 | 1 | 1 | appealed | ✅ |
| S12 | Same denial ingested twice -> second run is a no-op (idempotent) | noop | 1 | 1 | 1 | appealed | ✅ |
| S13 | Gmail fails once -> retried and succeeds, ledger updated | correct_resubmit | 1 | 1 | 0 | resubmitted | ✅ |
| S14 | Gmail fails twice -> plan fails, ledger NOT updated, team notified | correct_resubmit | 0 | 1 | 0 | submitted | ✅ |
| S15 | Rejected plan executes nothing | appeal | 0 | 0 | 0 | submitted | ✅ |
| S16 | Medical-necessity denial below the appeal threshold ($18) -> write off, not worth staff time | write_off | 0 | 0 | 0 | written_off | ✅ |
| S17 | Appeal window already closed (60-day payer, 3 days late) -> write off, no letter sent | write_off | 0 | 0 | 0 | written_off | ✅ |
| S18 | Clinical note missing from Drive -> escalate to billing, nothing sent | escalate | 0 | 0 | 1 | needs_review | ✅ |
| S19 | Unrelated email in the denials label -> flagged, left in inbox, world untouched | None | 0 | 0 | 0 | None | ✅ |
| S20 | No prior auth (CO-197) but note documents an emergency -> appeal under the emergency exception | appeal | 1 | 1 | 1 | appealed | ✅ |

Traces: one JSONL per scenario in `traces/<ID>.jsonl`.
