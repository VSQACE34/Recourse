"""Put the demo account back to its starting state so a live demo can be re-run.

  python -m scripts.reset_google            # ledger rows <- fixtures/ledger.csv, denials re-marked pending
  python -m scripts.reset_google --calendar # also delete agent-created calendar events (title starts with 'Appeal deadline')

Gmail: removes the '<label>-processed' label from every message under the denials label (sent appeals stay
in Sent; that's fine). Sheets: rewrites every ledger row from fixtures/ledger.csv. Drive: leaves appeals/ alone.
"""
from __future__ import annotations

import csv
import sys

from agent import config
from agent.models import LedgerRow
from agent.tools.google_apps import GCalendar, Gmail, Sheets, _http, get_creds

creds = get_creds()

# ---- ledger
sheets = Sheets(creds)
with (config.FIXTURES / "ledger.csv").open(encoding="utf-8", newline="") as f:
    rows = [LedgerRow(**{k: (v or "") for k, v in r.items()}) for r in csv.DictReader(f)]
for r in rows:
    sheets.update(r)
print(f"Sheets: {len(rows)} ledger rows restored from fixtures/ledger.csv")

# ---- gmail
gm = Gmail(creds)
q = f"label:{gm.label} label:{gm.done_label}"
res = gm.svc.users().messages().list(userId="me", q=q, maxResults=100).execute(http=_http(creds)).get("messages", [])
for m in res:
    gm.svc.users().messages().modify(userId="me", id=m["id"], body={"removeLabelIds": [gm._done_id], "addLabelIds": ["UNREAD"]}).execute(http=_http(creds))
print(f"Gmail: {len(res)} messages marked pending again")

# ---- calendar (optional)
if "--calendar" in sys.argv:
    cal = GCalendar(creds)
    ev = cal.svc.events().list(calendarId=cal.cal, q="Appeal deadline", maxResults=100).execute(http=_http(creds)).get("items", [])
    for e in ev:
        cal.svc.events().delete(calendarId=cal.cal, eventId=e["id"]).execute(http=_http(creds))
    print(f"Calendar: {len(ev)} agent events deleted")

print("Done. Restart the app so in-memory plans are cleared too.")