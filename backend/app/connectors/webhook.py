"""HTTP webhook connector.

Readings come in: a device or gateway POSTs them to /api/live/readings/{connector id}
as {"readings": [{"asset_id", "signal", "value"} | {"address", "value"}]}. Only the
signals listed in the blueprint are accepted, so a webhook can't silently override
an asset it doesn't own.

Commands go out: a confirmed command is POSTed as JSON to config.url:
    {"connector_id", "command", "asset_id", "value", "target_id", "address"}
Any 2xx counts as delivered.

Config: url, timeout_s (default 5), token_env (an environment variable holding a
shared token: sent as "Authorization: Bearer <token>" on commands, and required as
the X-Freeboard-Token header on pushed readings).

Only FREEBOARD_CONN_ variables can be named in token_env, and url must be http or
https on a host an operator allows (FREEBOARD_CONN_ALLOWED_HOSTS, see hosts.py). A
blueprint that breaks either rule gets a ValueError here, so the registry shows the
connector unavailable and nothing is ever read from another variable or sent elsewhere.
"""
from __future__ import annotations

import hmac
import threading
from typing import Callable, Optional

import httpx

from ..fieldmap import SIGNALS, FieldMap
from .base import Connector, Reading
from .hosts import check_url
from .signalmap import check_env_names, env_secret


class WebhookConnector(Connector):
    kind = "webhook"

    def __init__(self, spec: dict, fmap: FieldMap, now: Callable[[], float],
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        super().__init__(spec, fmap, now)
        check_env_names(self.config)
        self.url = self.config.get("url")
        if self.url is not None:
            check_url(self.url)
        self.timeout = float(self.config.get("timeout_s", 5))
        self.transport = transport  # tests inject httpx.MockTransport
        self.by_address = {addr: key for key, addr in self.signals.items() if addr}
        self._inbox: list[Reading] = []
        self._lock = threading.Lock()
        self.last_error: Optional[str] = None

    # ---- readings pushed in ----
    def authorized(self, token: Optional[str]) -> bool:
        want = env_secret(self.config, "token")
        if self.config.get("token_env") and not want:
            return False  # a token is configured but missing from the environment: refuse
        return not want or hmac.compare_digest(want, token or "")

    def push(self, readings: list[dict]) -> dict:
        accepted, rejected = [], []
        for r in readings:
            key = None
            if r.get("address") in self.by_address:
                key = self.by_address[r["address"]]
            elif (r.get("asset_id"), r.get("signal")) in self.signals:
                key = (r["asset_id"], r["signal"])
            if key is None or key[1] not in SIGNALS or "value" not in r:
                rejected.append({**r, "error": "not a signal this connector declares in the blueprint"})
                continue
            with self._lock:
                self._inbox.append(self.reading(key[0], key[1], r["value"]))
            accepted.append(f"{key[0]}.{key[1]}")
        return {"accepted": accepted, "rejected": rejected}

    def read(self) -> list[Reading]:
        with self._lock:
            out, self._inbox = self._inbox, []
        return out

    # ---- commands out ----
    def send(self, command: dict) -> dict:
        if not self.url:
            return {"ok": False, "detail": f"{self.id} has no url configured for commands."}
        address = self.commands.get((command.get("asset_id"), command.get("type")))
        body = {"connector_id": self.id, "command": command.get("type"), "asset_id": command.get("asset_id"),
                "value": command.get("value"), "target_id": command.get("target_id"), "address": address}
        headers = {}
        token = env_secret(self.config, "token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                r = client.post(self.url, json=body, headers=headers)
            if 200 <= r.status_code < 300:
                self.last_error = None
                return {"ok": True, "detail": f"Delivered to {self.id} (HTTP {r.status_code})."}
            self.last_error = f"HTTP {r.status_code}"
            return {"ok": False, "detail": f"{self.id} answered HTTP {r.status_code}."}
        except Exception as e:  # noqa: BLE001 - a dead endpoint must never break the console
            self.last_error = str(e)
            return {"ok": False, "detail": f"Couldn't reach {self.id}: {e}"}

    def status(self) -> dict:
        missing_token = bool(self.config.get("token_env")) and not env_secret(self.config, "token")
        problems = []
        if missing_token:
            problems.append(f"environment variable {self.config['token_env']} is not set")
        if self.last_error:
            problems.append(f"last command failed: {self.last_error}")
        detail = "; ".join(problems) or (
            f"receiving {len(self.signals)} signals" + (", sending commands" if self.url else ", no command url"))
        return {"id": self.id, "kind": self.kind, "available": not missing_token, "detail": detail}
