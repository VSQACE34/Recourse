"""Slack Socket Mode: receive button clicks over an outbound websocket, so the approval buttons work
without a public URL. Enable in the Slack app: Settings -> Socket Mode -> On (generate an app-level
token with connections:write -> SLACK_APP_TOKEN), and Features -> Interactivity & Shortcuts -> On
(no Request URL needed in Socket Mode).
"""
from __future__ import annotations

import threading
from typing import Callable

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse


def start(app_token: str, bot_token: str, on_action: Callable[[str, str, str], str]) -> SocketModeClient:
    """on_action(action_id, plan_id, username) -> replacement text for the card."""
    web = WebClient(token=bot_token)
    client = SocketModeClient(app_token=app_token, web_client=web)

    def handle(_client: SocketModeClient, req: SocketModeRequest) -> None:
        if req.type != "interactive":
            return
        _client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))   # ack within 3s
        payload = req.payload
        if payload.get("type") != "block_actions" or not payload.get("actions"):
            return
        action = payload["actions"][0]
        user = payload.get("user", {}).get("username") or payload.get("user", {}).get("name", "someone")
        text = on_action(action.get("action_id", ""), action.get("value", ""), user)
        container = payload.get("container", {})
        channel, ts = container.get("channel_id"), container.get("message_ts")
        if channel and ts:
            web.chat_update(channel=channel, ts=ts, text=text, blocks=[])       # replace the card, buttons gone

    client.socket_mode_request_listeners.append(handle)
    threading.Thread(target=client.connect, name="slack-socket-mode", daemon=True).start()
    return client