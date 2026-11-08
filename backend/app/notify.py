"""Phone push for alerts through ntfy (https://ntfy.sh). Off unless NTFY_TOPIC is set.

Ported from draft PR #21 (backend/app/alerts.py). Install the ntfy app and subscribe
to the topic. Use a long random topic name: anyone who knows it can read the alerts.

    NTFY_TOPIC   topic name (unset = no push, nothing leaves the machine)
    NTFY_SERVER  default https://ntfy.sh
    PUBLIC_URL   e.g. http://192.168.1.20:8000, so the notification can open the
                 console and acknowledge the alert
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

log = logging.getLogger("freeboard.notify")

PRIORITY = {"ALARM": "urgent", "WARNING": "high"}
TAGS = {"ALARM": "rotating_light", "WARNING": "warning"}


class Notifier:
    def __init__(self) -> None:
        self.server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        self.topic = os.environ.get("NTFY_TOPIC", "").strip()
        self.public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
        self._tasks: set[asyncio.Task] = set()
        self.last_error: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    def push(self, alert: dict) -> None:
        """Fire and forget. Never raises."""
        if not self.enabled or not alert.get("active") or alert.get("severity") not in PRIORITY:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._post(alert))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _post(self, alert: dict) -> None:
        headers = {"Title": f"{alert['kind'].replace('_', ' ').title()}: {alert['asset_id']}",
                   "Priority": PRIORITY[alert["severity"]], "Tags": TAGS[alert["severity"]]}
        if self.public_url:
            headers["Click"] = self.public_url
            headers["Actions"] = (f"view, Open console, {self.public_url}; "
                                  f"http, Acknowledge, {self.public_url}/api/alerts/{alert['id']}/ack, method=POST")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(f"{self.server}/{self.topic}", content=alert["message"].encode(),
                                      headers=headers)
                r.raise_for_status()
            self.last_error = ""
        except Exception as e:  # noqa: BLE001 - a failed push must never break the console
            self.last_error = str(e)
            log.warning("ntfy push failed: %s", e)
