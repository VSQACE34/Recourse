"""Slack adapter. pip install slack_sdk

Bot token scopes needed: chat:write, chat:write.public (or invite the bot to the channel),
plus reactions:read if you use the no-tunnel fallback.

Two ways to approve from Slack:
  A) Buttons  — Interactivity on, Request URL = https://<your-tunnel>/slack/interactions (needs ngrok/cloudflared)
  B) Reactions — react ✅ (white_check_mark) or ❌ (x) on the approval card, then POST /slack/poll-reactions
     (or run with APPROVAL_POLL_SECONDS set and the app polls itself). No public URL needed.
"""
from __future__ import annotations

from typing import Optional, Optional

from slack_sdk import WebClient

from .. import config


class Slack:
    def __init__(self, token: str = config.SLACK_BOT_TOKEN):
        self.client = WebClient(token=token)

    def post(self, channel: str, text: str, blocks: Optional[list[dict]] = None, idempotency_key: str = ""):
        res = self.client.chat_postMessage(channel=channel, text=text, blocks=blocks)
        return {"ts": res["ts"], "channel": res["channel"]}

    _chan_id: Optional[str] = None

    def _resolve(self, channel: str) -> str:
        """'#billing-denials' -> 'C0…'. Needs channels:read. Cached per adapter."""
        if not channel.startswith("#"):
            return channel
        if self._chan_id:
            return self._chan_id
        name = channel[1:]
        cursor = None
        while True:
            res = self.client.conversations_list(types="public_channel", limit=200, cursor=cursor)
            for c in res.get("channels", []):
                if c["name"] == name:
                    self._chan_id = c["id"]
                    return c["id"]
            cursor = res.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                raise ValueError(f"channel {channel} not found (is the bot invited, and does it have channels:read?)")

    def history(self, channel: str, limit: int = 15) -> list[dict]:
        """Recent messages, newest last. Needs channels:history + channels:read; bot must be in the channel."""
        cid = self._resolve(channel)
        res = self.client.conversations_history(channel=cid, limit=limit)
        out = []
        for m in reversed(res.get("messages", [])):
            out.append({"ts": m.get("ts"), "user": m.get("username") or m.get("user") or ("bot" if m.get("bot_id") else "?"),
                        "bot": bool(m.get("bot_id")), "text": m.get("text", ""),
                        "reactions": [r["name"] for r in m.get("reactions", [])]})
        return out

    def permalink(self, channel: str, ts: str) -> Optional[str]:
        try:
            return self.client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
        except Exception:  # noqa: BLE001
            return None

    def reactions(self, channel: str, ts: str) -> set[str]:
        """Names of emoji reactions on a message, e.g. {"white_check_mark", "x"}. Needs reactions:read."""
        res = self.client.reactions_get(channel=channel, timestamp=ts)
        msg = res.get("message", {}) or {}
        return {r["name"] for r in msg.get("reactions", [])}

    def update(self, channel: str, ts: str, text: str):
        """Replace a message (used to swap the approval card for a 'done' line after reaction approval)."""
        return self.client.chat_update(channel=channel, ts=ts, text=text, blocks=[])