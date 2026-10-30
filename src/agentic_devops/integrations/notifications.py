"""Microsoft Teams notifications via a Workflows ("When a Teams webhook request is received") URL."""

from __future__ import annotations

import logging
from typing import Protocol

import requests

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def notify(self, title: str, text: str, level: str = "info", url: str | None = None) -> None: ...


class NullNotifier:
    def notify(self, title: str, text: str, level: str = "info", url: str | None = None) -> None:
        log.info("[notify:%s] %s - %s", level, title, text)


class TeamsNotifier:
    """Posts an Adaptive Card. Failures are logged, never raised: notifications must not break a gate."""

    _COLOR = {"info": "Accent", "success": "Good", "warning": "Warning", "error": "Attention"}

    def __init__(self, webhook_url: str, timeout: float = 10.0):
        self.webhook_url = webhook_url
        self.timeout = timeout

    def notify(self, title: str, text: str, level: str = "info", url: str | None = None) -> None:
        card = {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4",
            "body": [
                {
                    "type": "TextBlock",
                    "text": title,
                    "weight": "Bolder",
                    "size": "Medium",
                    "color": self._COLOR.get(level, "Default"),
                    "wrap": True,
                },
                {"type": "TextBlock", "text": text, "wrap": True},
            ],
            "actions": [{"type": "Action.OpenUrl", "title": "Open", "url": url}] if url else [],
        }
        payload = {
            "type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
        }
        try:
            requests.post(self.webhook_url, json=payload, timeout=self.timeout).raise_for_status()
        except requests.RequestException:
            log.exception("Teams notification failed")
