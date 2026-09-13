"""Slack adapter. pip install slack_sdk

Bot token scopes needed: chat:write, chat:write.public (or invite the bot to the channel),
plus reactions:read if you use the no-tunnel fallback.

Two ways to approve from Slack:
  A) Buttons  — Interactivity on, Request URL = https://<your-tunnel>/slack/interactions (needs ngrok/cloudflared)
  B) Reactions — react ✅ (white_check_mark) or ❌ (x) on the approval card, then POST /slack/poll-reactions
     (or run with APPROVAL_POLL_SECONDS set and the app polls itself). No public URL needed.
"""
from __future__ import annotations

from typing import Optional

from slack_sdk import WebClient

from .. import config


class Slack:
    def __init__(self, token: str = config.SLACK_BOT_TOKEN):
        self.client = WebClient(token=token)

    def post(self, channel: str, text: str, blocks: Optional[list[dict]] = None, idempotency_key: str = ""):
        res = self.client.chat_postMessage(channel=channel, text=text, blocks=blocks)
        return {"ts": res["ts"], "channel": res["channel"]}

    def reactions(self, channel: str, ts: str) -> set[str]:
        """Names of emoji reactions on a message, e.g. {"white_check_mark", "x"}. Needs reactions:read."""
        res = self.client.reactions_get(channel=channel, timestamp=ts)
        msg = res.get("message", {}) or {}
        return {r["name"] for r in msg.get("reactions", [])}

    def update(self, channel: str, ts: str, text: str):
        """Replace a message (used to swap the approval card for a 'done' line after reaction approval)."""
        return self.client.chat_update(channel=channel, ts=ts, text=text, blocks=[])
