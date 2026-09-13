"""Runtime configuration, all overridable by environment variables."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

try:                                   # read ./.env if present, so nothing needs exporting
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


def today() -> date:
    """Frozen clock for evals; real clock otherwise. AGENT_TODAY=YYYY-MM-DD."""
    v = os.getenv("AGENT_TODAY")
    return date.fromisoformat(v) if v else date.today()


# Economics: an appeal costs staff time. Below this denied amount, don't bother.
MIN_APPEAL_AMOUNT = float(os.getenv("MIN_APPEAL_AMOUNT", "25"))

# Confidence gates for LLM judgements.
APPEAL_CONFIDENCE = float(os.getenv("APPEAL_CONFIDENCE", "0.70"))
ESCALATE_CONFIDENCE = float(os.getenv("ESCALATE_CONFIDENCE", "0.45"))

# Deadline within this many days -> urgent flag.
URGENT_DAYS = int(os.getenv("URGENT_DAYS", "7"))

# LLM
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "mock")          # mock | groq | openai_compat
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")               # openai_compat: e.g. https://openrouter.ai/api/v1
LLM_API_KEY = os.getenv("LLM_API_KEY", os.getenv("OPENROUTER_API_KEY", ""))
LLM_MODEL = os.getenv("LLM_MODEL", "")

# Practice identity (appears in letters)
PRACTICE_NAME = os.getenv("PRACTICE_NAME", "Clearwater Family Medicine")
PRACTICE_NPI = os.getenv("PRACTICE_NPI", "1234567890")
PRACTICE_EMAIL = os.getenv("PRACTICE_EMAIL", "billing@clearwaterfm.example")
BILLING_CHANNEL = os.getenv("SLACK_CHANNEL", "#billing-denials")

# Google
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS", str(ROOT / "credentials.json"))
GOOGLE_TOKEN = os.getenv("GOOGLE_TOKEN", str(ROOT / "token.json"))
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "")
SHEET_ID = os.getenv("SHEET_ID", "")
SHEET_TAB = os.getenv("SHEET_TAB", "ledger")
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary")
GMAIL_LABEL = os.getenv("GMAIL_LABEL", "denials")

# Slack
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")   # only for the HTTP /slack/interactions path
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")
# Evidence panel (hosted demo): share these view-only and the dashboard embeds them
SLACK_INVITE_URL = os.getenv("SLACK_INVITE_URL", "")
CALENDAR_EMBED_ID = os.getenv("CALENDAR_EMBED_ID", os.getenv("PRACTICE_EMAIL", ""))   # calendar id = the gmail address
EVIDENCE_TZ = os.getenv("EVIDENCE_TZ", "Asia/Kolkata")             # xapp-… enables Socket Mode buttons (no tunnel)