"""Alerts: one log, three channels.

1. WebSocket `alert` messages to every connected screen (always on).
2. Phone push through ntfy (https://ntfy.sh) when NTFY_TOPIC is set. Install the
   ntfy app, subscribe to the topic, and alerts arrive as real notifications with
   "Open console" and "Acknowledge" buttons. Use a long random topic name: anyone who
   knows it can read the alerts.
3. GET /api/alerts for anything that reconnects late.

Off by default so nothing leaves the machine unless you opt in.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import os

import httpx

log = logging.getLogger("freeboard.alerts")

SEVERITY_PRIORITY = {"CRITICAL": "urgent", "WARNING": "high", "INFO": "default"}
SEVERITY_TAGS = {"CRITICAL": "rotating_light", "WARNING": "warning", "INFO": "information_source"}


class Alerts:
    def __init__(self, broadcast) -> None:
        self._broadcast = broadcast
        self.items: list[dict] = []
        self.keys: set[str] = set()
        self._ids = itertools.count(1)
        self.ntfy_server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        self.ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()
        self.public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
        self._tasks: set[asyncio.Task] = set()

    def reset(self) -> None:
        self.items.clear()
        self.keys.clear()

    @property
    def push_enabled(self) -> bool:
        return bool(self.ntfy_topic)

    async def raise_alert(self, severity: str, title: str, body: str, *, key: str | None = None,
                          asset_id: str | None = None, clock: str | None = None,
                          ui: dict | None = None, push: bool = True) -> dict | None:
        """Create an alert once per key, broadcast it, and push it to phones."""
        if key and key in self.keys:
            return None
        if key:
            self.keys.add(key)
        alert = {
            "id": f"a{next(self._ids)}", "severity": severity, "title": title, "body": body,
            "asset_id": asset_id, "clock": clock, "acked": False, "acked_by": None, "ui": ui,
        }
        self.items.append(alert)
        await self._broadcast("alert", alert)
        if push and self.push_enabled and severity in ("CRITICAL", "WARNING"):
            task = asyncio.create_task(self._push(alert))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return alert

    async def ack(self, alert_id: str, by: str) -> dict | None:
        for a in self.items:
            if a["id"] == alert_id:
                if not a["acked"]:
                    a["acked"], a["acked_by"] = True, by
                    await self._broadcast("alert_ack", {"id": alert_id, "by": by})
                return a
        return None

    def open_items(self) -> list[dict]:
        return [a for a in self.items if not a["acked"]]

    async def _push(self, alert: dict) -> None:
        headers = {
            "Title": alert["title"],
            "Priority": SEVERITY_PRIORITY.get(alert["severity"], "default"),
            "Tags": SEVERITY_TAGS.get(alert["severity"], ""),
        }
        if self.public_url:
            headers["Click"] = f"{self.public_url}/phone?alert={alert['id']}"
            headers["Actions"] = (
                f"view, Open console, {self.public_url}/phone?alert={alert['id']}; "
                f"http, Acknowledge, {self.public_url}/api/alerts/{alert['id']}/ack, method=POST"
            )
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(f"{self.ntfy_server}/{self.ntfy_topic}",
                                      content=alert["body"].encode(), headers=headers)
                r.raise_for_status()
        except Exception as exc:  # a failed push must never break the console
            log.warning("ntfy push failed: %s", exc)
            await self._broadcast("push_failed", {"alert_id": alert["id"], "error": str(exc)})
