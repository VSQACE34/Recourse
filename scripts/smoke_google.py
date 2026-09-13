"""Prove each real adapter works before the night. Run after filling .env (TOOLS=google).

    python -m scripts.smoke_google
"""
from __future__ import annotations

from agent import config
from agent.tools.google_apps import Drive, GCalendar, Gmail, Sheets, get_creds
from agent.tools.slack_app import Slack

creds = get_creds()
print("OAuth ok")

d = Drive(creds)
print("Drive: notes/C-1003.md ->", d.read_text("notes/C-1003.md")[:80].replace("\n", " "), "…")

print("Drive write:", d.write_text("appeals/_smoke_test.txt", "safe to delete", "smoke"))

s = Sheets(creds)
rows = s.all()
print(f"Sheets: {len(rows)} ledger rows; first =", rows[0].claim_id if rows else None)

g = Gmail(creds)
unread = g.list_unread()
print(f"Gmail: {len(unread)} unread under label '{config.GMAIL_LABEL}'")

c = GCalendar(creds)
ev = c.create_event("denial-agent smoke test", config.today().isoformat(), "safe to delete", "smoke-test")
print("Calendar:", ev)

if config.SLACK_BOT_TOKEN:
    print("Slack:", Slack().post(config.BILLING_CHANNEL, "denial-agent smoke test — hello from the bot"))
else:
    print("Slack: SLACK_BOT_TOKEN not set, skipped")
